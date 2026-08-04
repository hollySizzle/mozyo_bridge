# Managed State Model (desired-state event log と正本境界)

Redmine #11695 / #11697。mozyo-bridge 管理下の session / workspace について、**desired state / managed lifecycle** を replay 可能な event log として正本化し、**observed liveness / handoff target existence** は live tmux runtime を正本のまま維持するための設計正本。設計経緯は #11639 journal #56299 (Design Consultation) / #56318 (Claude Answer) / #56330 (Codex synthesis)、依存棚卸しは #11696 journal #56361。

本 doc は **正本境界の集約点** である。既存 `session-inventory.md` / `workspace-registry.md` / `otel-event-store.md` は本 doc を参照し、判断材料を二重化しない (docs catalog governance)。

## 正本境界 (4 層モデル)

```yaml
正本マップ:
  workspace_identity:        # workspace_id / canonical session / display path / preset
    正本: home registry (registry.sqlite) -> workspace anchor -> path derivation
    根拠: #11429。registry commandが唯一の書込面。event log / projection に二重化しない
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
| `registry.sqlite` | v1 | `workspace register` / guarded `init` / exact stale-row `workspace retire` | `workspace_identity` 正本。`workspaces` と `workspace_activity` | identity 正本なので write path は corrupt / unknown schema で die。retireはverified backup-first + exact row/digest + live-zeroに限定。operator が退避し、workspace anchorまたはretire backupから復旧する。read path は anchor / derivation へ degrade |
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
| `workspace_identity` | `registry_*` | `workspace_registry`。write は `workspace register`、guarded `init`、exact stale-row `workspace retire` のみ | session/name resolver、inventory identity join、doctor、target preflight | authoritative。backup / restore / workspace anchor re-register で復旧。drop/rebuild 自動化は禁止 | runtime pane/process/cwd、presentation grouping、workflow completion を持たない |
| `desired_state` | `managed_events`, future `managed_unit_events` | command boundary append。session/window/pane/unit を作成・採用・mark・rename した command が writer | recovery suggestion、presentation current table rebuild、doctor/audit | append-only lossy。partial corruption は quarantine + gap marker。identity/liveness は依存しない | current liveness / handoff availability / completion truth にしない |
| `desired_current_state` | future `unit_*`, `presentation_*` | future presentation/unit commands。event fold or explicit command が writer | cockpit/grouping UI、launch planning、doctor/rebuild suggestion | rebuildable if source events are sufficient; otherwise operator-managed current stateとして backup restore。destructive auto reconcile 禁止 | routing authority、live target existence、private operator policy default にしない |
| `last_observed_projection` | `inventory_*`, future `target_observations` | runtime listing / reload / observation command が snapshot replace | UI/diagnostics/candidate display、doctor | `rebuildable_cache`。drop/rebuild 可。ただし stale / observed_at / source を必ず出す | action permission、healthy/current 判定、pane existence 正本にしない |
| `activity_timeline` | `otel_*` | loopback receiver single writer | activity model、event timeline、cockpit activity join、doctor | `rebuildable_cache` / best-effort。receiver down 中の loss を許容。retention/prune 可 | prompt/content storage、death/liveness truth、workflow progress truth にしない |
| `runtime_current_fact` | DB table なし | live tmux / future sidecar live query | side-effecting command preflight、handoff resolver、runtime observation reload | persisted recovery 対象ではない。読めなければ unknown / fail closed | DB snapshot や UI 表示へ正本委譲しない |
| `side_effect_permission` | DB table なし | mozyo command implementation | command execution only | persisted state + durable workflow gate + live preflight から毎回計算 | raw DB reader / UI / private consumer が直接許可判定しない |
| `workflow_truth` | DB table なし | Redmine / governed workflow | agent coordinator、review/close gate、doctor links | Redmine durable recordで復旧。runtime DB へ複製しない | DB health / event freshness から completion/approval を導出しない |

### writer rules

- `workspace retire` (#14877) は一般的なregistry deleteではない。dry-runを既定とし、対象workspace id、
  `updated_at`、private record全体のdigest、missing path、current workspaceでないこと、global Herdr inventoryの
  lossless readとtarget live agent 0をcanonical planへ拘束する。executeはdirect ownerが承認したexact
  plan digestを再提示した場合だけ、同じauthorityを二度読みし、SQLite backup APIでprivate
  `${MOZYO_BRIDGE_HOME}/workspace-registry-backups/<plan_digest>.sqlite` を作成・検証してから
  exact rowをtransactional deleteする。backupはsingle-link regular file・mode 0600・live registryと異なる
  device/inodeを要求し、schemaはbackupと `BEGIN IMMEDIATE` 後の双方で再検証する。`workspace_activity` は
  foreign-key cascade、削除後は同一transaction内readback、同じdigestのverified backupがあるreplayは
  registry rowとorphan activityの双方が不在の場合だけzero-write `already_retired` とする。path present、current
  identity、live/unknown inventory、record/plan drift、backup/readback不成立は全てtyped refusalである。
  public payloadへcanonical/display/backup absolute pathを出さない。tmux cacheはcurrent Herdr rolloutの
  live consumer authorityへ昇格させない。
  回復時はbackup DBを現行registryへ丸ごと上書きしない。退役対象pathが復元できる場合は、そのpath内の
  workspace anchorを正本として `workspace register` を再実行し、元のidentityを復元する。backupはprivateな
  監査・障害解析用証拠として保持し、現行registry全体を過去snapshotへ巻き戻す用途には使わない。

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
    presence は従来どおり live inventory (`observed_liveness`) を読む。`released` は release の
    **完了**であり、`partial` は close 未完 (再 drive 要) の別 state として区別する。hibernate の
    「clean success」判定 (`is_success`) と CLI exit は `process_release == released`（または live
    slot 無しの `not_requested`）に bind し、`partial` を fully-actuated success として報告しない
    (#13843)。
  - **hibernate release-boundary TOCTOU 保全 fence** (#13843)。hibernate の preflight (clean/idle/
    fingerprint 取得) と managed process release は **atomic でない**。preflight snapshot と release の
    間に worker が worktree mutation を開始でき、lane が `hibernated`/`released` でも durable boundary
    record 無しの dirty residue が残る。actuation は preflight (T0) で状態 snapshot を取り、**release
    直前 (T1)** に同一 fresh snapshot で ①worktree fingerprint (tracked 内容+untracked 内容に感応な
    digest、porcelain 行だけでない)、②worker running / pending composer の live 観測 (#14065 provider
    ghost gate で ghost-refine。#14239: gate へ渡す facts reader の arity 違反で refinement が silent 無効化され
    dim ghost が `composer_pending_real` に誤分類されていたのを修正し、公開 `composer-render` 診断と同一の
    authority-resolved live read へ統一)、③live managed
    slot (`assigned_name→locator`)、④lifecycle revision、⑤(project-gateway) exact declared generation
    + startup attestation を **再検証**し、preflight から drift したら **lifecycle transition 0 /
    process close 0 の typed blocked**。CAS→partial→retry は idempotent で redrive も同じ fence を通す
    (dirty residue を clean と記録しない)。release 後 (T2) の post-check で unexpected worktree residue を
    検出したら **success を withhold** し (lane は `hibernated` のまま、issue/worktree/branch/commit を
    保存)、durable recovery/boundary-record path へ収束させる。fingerprint / activity / attestation の
    live 観測が **読めない場合は fail-closed** (clean と誤認しない)。実装契約の詳細は
    `sublane_hibernate*.py` の module docstring と IR anchor を読む。
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
  - **lane 幾何 kind (lane-role pane placement の heal 正本)** (schema v7、#13647 Tranche 1b /
    Design Answer j#85645 / disposition j#85650 P1)。追加 field `lane_kind`
    (`coordinator` | `delegated_coordinator` | `implementation` の canonical 3-token。alias 無し)。
    - **何を保存するか**: lane が **create された時点で創出側 caller が governance から解決した**
      delegation-tree 上の位置 (親 / 子 / 孫)。placement geometry 軸であり、mzb1 name /
      `MOZYO_AGENT_ROLE` (= provider) / route / attest / retire authority には一切昇格しない。
    - **なぜ authority row に置くか**: heal (再 launch) が **同じ pane geometry を offline で**
      再現するため。launch path (`prepare_session`) は pure orchestration であり、heal のたびに
      Redmine を読み直す network 依存を launch chokepoint へ持ち込まない。`lane_metadata` /
      display projection は自身の docstring で "never routing authority / read fail-open" を宣言する
      **display join** なので、そこへ role 列を足して launch authority へ昇格させない
      (この誤設計は j#85644 → 正本 j#85645 で棄却済み)。
    - **generation-bound / immutable**: 世代内では不変。governance 上の再結線は
      `open_next_generation(lane_kind=...)` という **明示 CAS の generation 境界でのみ** 起きる
      (`None` = 現世代の値を carry forward、明示 `""` = 「durable kind fact 無し」への retract)。
      ordinary な disposition transition は `lane_kind` を書き換えない。
    - **declaration identity の一部**: 異なる kind を載せた re-declare は idempotent no-op ではなく
      **divergent re-declare** (`already_declared`、zero-write)。既存 geometry authority を後発
      caller の推測で上書きさせない (`lane_metadata.upsert` の tombstone 復活 anti-pattern と同型)。
    - **fail-closed vocabulary**: 非 canonical token は store へ届く前に `LaneKindError` で zero-write
      拒否。`declared_slots` には混載しない (あちらは launch 後 observation、こちらは pre-launch 宣言)。
    - **migration / fallback**: pre-v7 row は additive backup-first migration で **空** `lane_kind` を
      得る (推測値を書かない)。空 = durable kind fact 無し → launch は従来どおり `lane_class`
      (`default` / `sublane`) 幾何へ byte 一致で fallback する。
    - **launch 時の 2 authority と矛盾時の扱い** (`herdr-native-identity.md` §5.2): fresh launch は
      caller-supplied `LaneLaunchContext`、heal は本 field。両方あり **不一致**なら片方が stale なので
      launch は **zero side effect で fail-closed 拒否** (どちらかを黙って採用しない)。
  - **hibernate liveness 境界 (resume の immutable timestamp 境界。世代証明ではない)**
    (schema v8、#14477 / live evidence #14476 j#88614-j#88618)。追加 field `hibernated_at`。
    semantics と「代替境界を与えない」理由の実装正本は `core/state/lane_hibernation_anchor.py`。
    - **何を保存するか**: lane が `hibernated` へ入った **transition の時刻**。resume (#13682) は
      各 slot の startup self-attestation (#13637) が **この時刻より厳密に後** に観測されたことを
      要求する。**これは liveness 境界であり世代証明ではない** (#14477 review j#94531 R2-F1 /
      disposition j#94544 A.3): locator は pane-id なので survivor も自分の pre-hibernate
      attestation に一致し、かつ timestamp 自体も 3 vector (caller 供給 CAS stamp の backdate /
      host clock 後退 / 自己申告 `observed_at`) で破れる。**clock 非依存の判別は下記
      released-locator fence が担う**。
    - **なぜ `updated_at` と分けるか**: `updated_at` は **全ての write が動かす** column である。
      metadata-only な write が境界を前へ押し、その write 自身が直前に検証した live pair を
      `stale_generation` に落とす。実測は `repair-pins` (#13879、declared-pin 補填 + revision /
      decision anchor 更新のみで launch / close / resume / send を一切しない) が、まさに検証した
      exact pair を resume 不能にし、glass-break しか残らなかった (#14476 j#88617-j#88618)。
      責務分離が fix であり、gate の緩和ではない。
    - **write するのは 1 event だけ**: `hibernated` へ入る disposition CAS のみが stamp する。
      revision bump / decision anchor / declared-pin repair / release・replacement write /
      reconcile phase は **触らない**。`active` へ戻る側 (resume rehydrate / recovery lane 昇格 /
      `open_next_generation`) は **空へ clear** する (起きている lane に境界は存在しない)。
      terminal (`superseded` / `retired`) は audit fact として保持する。
    - **clear が fail-closed 方向である理由**: 将来 stamp しない writer が row を `hibernated` へ
      戻した場合、古い境界が残っていれば threshold が過去に飛び **gate が緩む**。空なら境界不在
      として refuse されるので、緩まない。
    - **anchor が空の row に代替境界を与えない** (#14477 review j#94515 F1 / verdict j#94520)。
      pre-v8 row は additive backup-first migration で **空** anchor を得る (`updated_at` から
      back-fill **しない** — metadata write の stamp を immutable column へ焼き付けると、本 version
      が除去する false `stale_generation` が恒久化する)。**読取側も `updated_at` を代用しない**。
      - **理由**: `updated_at` は **単調ではない**。本 component の全 CAS は caller 供給の `now` を
        受け、prior stamp 以上であることを検証する writer は 1 つも存在しない。よって backdated な
        programmatic caller や wall clock の後退 (NTP step-back / host clock skew) で `updated_at`
        は真の hibernation より **前** になりうる。実測 (j#94515 F1) では
        `updated_at(19:40) < survivor attestation(19:50) < true hibernate(20:00)` が成立し、
        **真の pre-hibernate survivor が admit され lane が active 化した**。
      - 「`updated_at >= hibernated_at` だから fallback は常により厳しい」という以前の主張は
        **撤回**する。この不等式が成立するのは **anchor を持つ row** = fallback を使わない row
        だけであり、anchor が空の row には何も保証しない。
      - **安全な代替は存在しない**: `created_at` は真の境界より更に前 (より弱い)、release
        generation に timestamp column は無い、今後 monotonic invariant を強制しても **旧 build が
        既に書いた row** には遡及できない。選択肢は「refuse」か「推測」であり、世代証明は推測して
        はならない。
      - **帰結 (運用上の機能後退として明示)**: pre-v8 build が hibernate した lane は、v8 の
        hibernate transition を経るまで standard resume rail で resume **できない**。row の
        **読取**は不変 (`get` / `records` / 非migrating read) であり、失われるのは freshness
        **証明** のみ — その row について証明は元々存在しなかった。
    - **現在時刻は使わない**: 「最近観測された」pane を通してしまい、世代証明の逆になる。
      anchor を欠く row は `unavailable` として **freshness 半分を fail-closed** にする
      (threshold 不在を「比較対象が無いので fresh」と読ませない。`evaluate_pair_attestation` は
      空 threshold で freshness 比較を skip するため、caller 側で明示的に倒す必要がある)。
    - **timestamp は liveness 境界であり、世代証明ではない** (#14477 review j#94531 R2-F1 /
      coordinator disposition j#94544 A.3)。**どの clock を信じるかを変えても閉じない** vector が
      3 つある: ① hibernate CAS の stamp は caller 供給 `now` で、prior stamp との比較を検証する
      writer が 1 つも無いため backdate できる、② host wall clock の後退 (NTP step-back / skew) は
      trusted clock でも同じ順序を生む、③ `observed_at` は attest する process 自身が書く。
      実測 (j#94539): anchor を backdate すると true survivor が admit され lane が active 化した。
    - **世代証明は clock 非依存の released-locator fence** (`core/state/lane_released_locator_fence.py`)。
      hibernate の release は **閉じた exact locator** を `release_pins` に authority grade で残す。
      survivor は tmux pane-id を保持するのでこの集合に入り、genuine relaunch は入らない。よって
      上記 3 vector すべてに依存せず拒否できる。
      - **証拠不在 / 判読不能 / 不完全は refuse** (A.2)。row だけでは「hibernate 時に process 0」と
        「release 証拠を残さず survivor がいた」を区別できない。**証拠不在を fresh と推定しない**。
        `process_release` が `released` に到達していない (never requested / in flight) 場合、pin が
        判読不能な場合、released locator が observed slot 数を下回る場合はいずれも拒否する。
        これは release 証拠を残さず hibernate した lane に対する **意図した機能後退**である。
      - **完全性を pin の `role` で判定しない**。`release_pins` の role 語彙は caller 間で不統一
        (lane role `gateway`/`worker` と provider 名が混在) なので、role 基準は誤判定する。
        distinct locator 数で判定する。
      - **pane-id 再利用による false refusal は安全方向として受容** (A.4)。tmux が pane-id を
        再利用すると genuine relaunch が拒否され得る。typed detail に `released_locator_reuse` を
        出すので、operator は真の survivor と切り分けられる。
      - resume は **この fence と既存の attestation / provider / multiplicity / declared-pin fence
        すべて**を満たしたときのみ可 (A.3 / A.6)。いずれも緩めない。
      - **fence の権威は `release_pins` 単体ではない** (#14477 review j#94570 R3-F1 →
        disposition j#94582 A″)。旧実装は locator 件数で「release が完全か」を判定していたが、
        件数は完全性を証明しない: **別 locator 2 件を記録すれば count は充足し交差も起きず、
        true survivor が admit される**ことを実測した。根本原因は `release_pins` が
        **caller 供給**だったこと — timestamp と同型の欠陥である。
  - **lane epoch (hibernate 世代の単調 counter)** (schema v10、#14756)。追加 field `lane_epoch`。
    実装正本は `core/state/lane_epoch.py`。
    - **何を保存するか**: その lane が hibernate した回数 = 発行済み世代番号。**`hibernated` へ
      向かう disposition CAS だけ**が **格納値から** 発行する (増分は strict decode 後に Python 側で
      算出し canonical decimal TEXT を書く。**SQL 算術は使わない**)。
      **malformed な格納値からは発行しない** — malformed とは **canonical decimal TEXT 以外すべて**、
      すなわち noncanonical TEXT (符号付き `+0` / 前置ゼロ `00` / 空白付き `" 0 "` / 小数 `0.0`) /
      INTEGER / REAL / BLOB / `NULL` / `bool` / negative である (**canonical decimal TEXT は valid**)
      (#14756 j#96881 F2 / j#96911 F2)。「未発行 (canonical decimal TEXT `"0"`)」と「読めない」は別の事実であり、
      read 側は両方 `lane_epoch_authority_unavailable` へ畳んでよいが、**writer が畳むと
      counter rollback になる** — corrupt 値から 1 を mint すると、既に release 済みの世代が
      持つ epoch を再発行してしまう。非 hibernate 遷移も corrupt 値を `0` へ正規化しない
      (無関係な遷移経由の laundering も laundering である)。malformed を観測した CAS は
      **zero-write の typed refusal** とし、adoption も **canonical TEXT `"0"`** のみ許可する。
      malformed の集合は「canonical decimal TEXT 以外すべて」= INTEGER / REAL / BLOB / `NULL` /
      `bool` / 符号付き / 前置ゼロ / 空白付き。列は `BLOB` (NONE affinity) なので storage class
      がそのまま返る。**successor が canonical 境界を越える場合も mutation 前に zero-write
      refusal** とする (#14756 j#96973 R11-F10)。
      caller に epoch parameter は存在しない。timestamp (v8) と caller 供給 pins (pre-v9) が
      どちらも世代証明にならなかった根本原因は「caller が値を供給できる」ことだったので、
      **供給する seam 自体を作らない**。
    - **`0` は未発行であって閾値ではない**。pre-v10 row / 未 hibernate lane は `0` で migrate し、
      `lane_epoch_authority_unavailable` として **fail-closed** する。`lane_generation` からの
      backfill は禁止 (あれは re-incarnation の counter であり hibernation の counter ではない。
      seed すると本 build 以前に launch された process と一致する epoch を発行してしまう)。
    - **`active` へ戻る際に clear しない**。`hibernated_at` は clear するが、これは
      「有効な境界」と「単調 counter」の違いである。counter を reset すると旧世代 process が持つ
      epoch を再発行し、`release_observation` / locator fence を通り抜ける survivor を作る。
      clear する 3 writer (`transition_disposition` rehydrate / `supersede_and_activate` /
      `open_next_generation`) は本 field を **UPDATE 列に含めない**ことで保存する。
      R4 review (j#94707 R4-F1) は「field を足したら reset する writer も同数ある」を検出したが、
      本 field はその **逆向きの義務** — reset してはならない writer を列挙する — を負う。
    - **消費**: managed launch が read-only reader で読み `MOZYO_LANE_EPOCH` として注入し、agent が
      自 env で観測した raw token を startup attestation (v3) に記録する。resume は両 slot の
      attested epoch が hibernate epoch より strictly newer のときのみ admit する。詳細と
      fail-closed vocabulary は `vibes/docs/specs/herdr-native-identity.md` §2 を読む。
    - **launch path は read のみ**。epoch は hibernate CAS でしか発行されないので、launch は
      write も migration も増やさない (`#### read-compatible / write-migrating split` の
      「read は migrate しない」契約を維持する)。store が読めない場合は注入せず launch は
      #14756 前と byte-invariant で、拒否は resume 側の typed reason に出る。
    - **replacement path の拒否は close より前に置く** (#14756 j#96848)。replacement は old slot を
      close してから new slot を launch するので、epoch/store 互換性を launch step で判定すると
      「typed に拒否したが pair は既に壊れている」状態になる (実測: herdr start は 0 だったが
      close と participant CAS は 0 ではなかった)。判定は `ReplacementActuatorUseCase` の
      owed-step dispatch **より前** の `store_admission` seam で行い、refusal 時は
      zero-close / zero-CAS / zero-launch とする。crash replay も同じ dispatch 点を通る。
      - 判定は `herdr_launch_epoch.replacement_store_admission` **1 箇所**が答える。launch
        preflight と同じ `launch_carries_lane_epoch` + `epoch_store_admission` を使い、
        epoch 軸**だけ**を見る (launcher capability 軸は subprocess probe が要るので分離)。
      - **action 単位で評価する**。actuate 対象の participant だけでなく、`replaced` でない
        participant **全て**を見る。1 つでも inadmissible なら action 全体を拒否する。
        participant 単位にすると「A を close/relaunch した後に B が戻れないと判明する」——
        この fence が防ぐはずの半壊 pair に 1 step 遅れて到達する。
      - **6 construction site すべてに注入する**。seam を作っただけで注入しないと本番では
        未発効であり、regression は green のまま欠陥が残る (j#96854 実測: 注入 0/6)。
        3 つの ops protocol は本 member を **required** として宣言し、実装漏れが silent な
        un-gated path ではなく error になるようにする。`self_close_executor` は passthrough
        のみで未 arm — src に composition root が無いことを実測した上での判断であり、
        その理由を call site に明記する (省略と判断を区別する)。
    - **legacy recovery は plan のみで、実行 rail を持たない** (#14756 j#96861 / j#96866)。
      pre-epoch build が hibernate した lane を前進させる手順は close-first に固定する
      (`lane_epoch_legacy_recovery_plan.CANONICAL_STEPS`): 旧 pair 両 slot を terminal close →
      attested-live intersection=0 の fresh 確認 → backup-first store migration + strict
      readback → lifecycle epoch `0→1` adoption → v3/native epoch1 で fresh pair relaunch。
      adoption を先に置くと crash 時に `epoch=1` + v1 store + live old pair が残り、上記の
      pre-effect fence が次の close を拒否する **自己 deadlock** になる。
      - ただし **この sequence を実行する rail は置かない**。j#96866 が実環境で計測した
        attested-live intersection は **5 workspaces / 18 agents** (実行主体たる coordinator を
        含む) であり、target pair だけ close しても migration は `blocked_consumers_live` で
        止まる。close してから壁に当たる実装は、救うはずの pair だけを壊して partial
        shutdown を作る。
      - したがって lane-local rail は **target pair 以外の consumer を観測した時点で**
        typed `offline_global_runtime_upgrade_required` を返し、zero-close / zero-CAS /
        zero-migrate とする。census は lane の shape 判定**より前**に評価する
        (fleet の事実が「そもそも可能か」を決めるため)。
      - **除外集合 (= 自 lane の pair) は caller 入力から取らない** (#14756 j#96881 F1)。
        caller supplied な slot 名を無条件に census から引くと、**全 consumer 名を渡すだけで
        global blocker を消して `plan_ready` にできる** (実測)。authority は
        **current generation の `declared_slots`** であり、exact 2 slot を decode して
        live inventory と startup attestation へ workspace / lane / role / assigned_name /
        **locator** を byte-exact join する (j#96890 §2: attestation 側 locator を join
        しないと stale row が recycled pane を「自分のもの」にしてしまう)。
      - **`release_observation` / release pins を current pair authority の代替にしない**
        (#14756 j#96895)。実 #14755 row は observation length 0 / `declared_slots` length 532
        であり、observation を authority にすると **named acceptance 対象がまず refuse される**。
        observation は original release 時点の snapshot で、bootstrap / pin repair 後の
        current pair ではない。過去 evidence として矛盾診断に使うのは可。
      - declared slots が absent / corrupt / **exact 2 slot でない** / role 重複 / locator 不一致
        の場合は typed refusal。**部分集合だけを除外しない** (半分の pair を pair として扱うと、
        不完全な答えで census を狭める)。CLI flag は `--assert-slot` = **assertion 専用**で、
        導出集合と exact 一致しなければ refuse する。供給源にはしない。
      - 全 workspace drain / callback supervisor 停止 / attestation store migration /
        lifecycle v9→v10 backup-first migration / new runtime install / top coordinator
        first relaunch を含む **global rollout は別の durable work unit** であり、fleet の
        外側の非 consumer 主体を必要とする。本 plan はその window の中でだけ有効。
    - **global offline rollout は plan と外部 one-shot execution を分離する**
      (#14838 j#97096 / execution rail)。public rail は
      `mozyo-bridge herdr offline-rollout plan|delegate|run|status` の4 verb。
      `plan` は副作用ゼロ、`delegate` は既定でpreflightのみ、`run` はconsumer外runnerへ
      固定した内部actuation面であり、両mutation verbともliteral `--execute` がなければ
      zero-writeで拒否する。TestPyPI publishは本railの責務外で、既に存在するexact artifactだけを
      download/verify/installする。
      - plan は home registry の **全 workspace id / project name**、global Herdr inventory の
        **全 managed assigned name**、current top identity、両 supervisor の secret-safe status、
        attestation / lane lifecycle / startup transaction の **3 store schema + logical content
        digest**（startupはpublic migration primitiveのmigration-plan digestも含む）、各 worktree の
        content-sensitive WIP fingerprint を収集する。絶対 path、credential、pane text、WIP bytes は
        public JSON に含めない。project name が current project と異なる workspace は
        `unrelated_project` と明示し、黙って rollout 対象外にしない。
      - registry / inventory / WIP / store / supervisor は前後 2 回 read し、canonical projection が
        1 byte でも異なれば `snapshot_drift` で拒否する。unreadable / unknown schema、未登録
        workspace を指す agent、duplicate assigned name、unmanaged inventory row、top identity の
        exact-one join 不成立も **plan 無し**で拒否する。read-only SQLite probe が WAL の `-shm`
        index を materialize しうる既知の bookkeeping 例外を除き、authority row / schema / process /
        worktree を変更しない。
      - canonical plan は top 以外を先に stop、top を最後に stopし、restore は top を最初にする。
        schema transition は attestation `→v3`、lane lifecycle `→v10`、startup transaction `→v2`
        を明示する。artifactのsource SHA / `refs/heads/<branch>` / workflow run id / wheel SHA-256 /
        sdist SHA-256の5者がすべて揃う場合だけ `exact_pin_ready=true` とする。不足pinがあっても
        plan作成は成功するが実行承認には使えず、形式不正なpinはplan無しで拒否する。全pinは
        canonical plan bodyとdigestへ含める。plan body の sorted compact JSON を SHA-256 して
        `plan_digest` とし、後続の owner approval は exact digest・全 workspace/assigned name・
        unrelated classification・schema transition・artifact pin・global stop・forward-only を列挙する。
        旧 runtime への rollback は認めず、失敗時も新 runtime で前進復旧する。
      - `plan --json` は上記列挙をcanonicalな2行approval noteとして併記する。`delegate` はfresh
        planを再生成してdigest一致を要求し、`ISSUE:JOURNAL` のjournal本文がその2行とbyte-exact、
        かつjournal authorがissue authorと一致する場合だけdirect owner approvalと認める。prose、
        wrapper、末尾改行、非canonical JSON、別digest/targetはapprovalではない。
      - `delegate --execute` は全workspace pathと全agent locatorをpublic planへ混ぜずprivateに
        再captureし、planのidentity集合とexact一致させる。`${MOZYO_BRIDGE_HOME}/
        offline-rollout-actions-v1/<action-id>/` (0700、record 0600、payload SHA-256 seal、exclusive
        nonblocking lock) にactionをreserveしてから、candidate wheelをexact SHA-256で検証した
        独立venvとRunAtLoad/KeepAlive=falseのone-shot LaunchAgentを準備・bootstrapする。更新対象
        pipx env自身をrunnerにしない。
      - `run --execute` はLaunchAgentが注入したexact action tokenを持ち、かつ
        `MOZYO_AGENT_ROLE` / `MOZYO_WORKSPACE_ID`を持たないconsumer外processだけをadmitする。
        actionのphase prefixと各readback receiptを1 phaseごとにatomic保存し、crash/retryは未完
        phaseから前進する。phase順はplanの
        `supervisor stop → non-top stop → top stop → zero → backup → 3 migration → exact install →
        top first → remaining → supervisor pair → final verify`から変更しない。
      - 最初のagent closeより前に、dirty workspaceごとにtracked/index binary patch、Git index、
        untracked path/archiveをprivate directoryへ保存し、planのcontent-sensitive WIP digestを
        再照合する。stash/reset/checkoutは行わない。closeはplan時のassigned-name + locatorの
        exact generationだけを対象とし、同名different locator、foreign/unmanaged row、inventory
        unreadableはzero-progress refusal。既にabsentなexact planned nameはcrash replayとしてのみ
        goal stateへ畳み、consumer=0をfresh raw/projection countの連言で確認する。
      - backupはattestation/state container/startup seal+DBをSQLite logical snapshotとreadbackで
        検証する。migrationは既存public primitiveをcomposeし、startup v2 replayはprivate actionの
        typed completion receipt + recovery artifact再検証を必須にする。installはprivateに保持した
        exact wheelをpipxへ渡し、通常PATH executableのversionをreadbackする。restoreはtop unitを
        先に`herdr session-start`し、各nameをfresh inventory locator + v3 self-attestationでjoinして
        からremaining unitへ進む。supervisorはpair install/statusの両方で2 agent loaded、home pin、
        executable、credential readinessを確認する。
      - `status` はaction/plan/phase/reason/timestampだけを返し、workspace path、locator、WIP bytes、
        backup path、credentialを公開しない。status read成功は途中/blockedでもcommand successで、
        rollout完了は別field `completed` で表す。
  - **release generation observation (release が閉じた slot 集合の immutable 正本)**
    (schema v9、#14477 disposition j#94582)。追加 field `release_observation`。実装正本は
    `core/state/lane_release_observation.py` / `core/state/lane_release.py`。
    - **何を保存するか**: release driver が live inventory から **1 度だけ列挙**した exact
      observed slot snapshot。store は **この snapshot から release pins を単一導出**し、
      caller から別値を受け取らない。raw `pins=` public API は **authority seam として残さず
      fail-closed** (後方互換なし)。**拒否は typed domain error (`ReleaseObservationError`) で
      あること**が契約で、arity error ではない: `observation` を required parameter にすると
      literal legacy 呼出 (`pins=` のみ) が argument binding 段階の `TypeError` になり、seam が
      説明を返せないまま落ちる (j#94707 R4-F2 が実測)。`observation` 省略も同じ typed error で
      拒否する。
    - **absent と complete-empty を literal に区別する**。`''` = **absent** (legacy / 未記録) は
      「証拠が無い」であり resume は fail-closed。記録済みの **complete-empty**
      (`{"v":1,"slots":[]}`) は「driver が見て live slot 0 だった」という **positive evidence**
      で、resume は許可しうる。両者を「空」に畳むのが、本 issue の review chain が繰り返し
      検出した「証拠不在を証拠と読む」誤りである。
    - **writer gate と read gate の双方で完全一致を検証**する。observation と格納 pins が
      食い違う row は proof に使えない (欠け = 未証明 slot、過剰 = 観測していない close の主張)。
    - **generation 中は write-once**。metadata repair / decision / revision / outcome writer は
      変更しない。新 release generation の開始時のみ exact replacement。`active` へ戻る際は
      release 一式と共に clear される。**clear する writer は 3 経路**で、いずれも
      `process_release` / `release_action_id` / `release_pins` / `release_observation` を
      **同一 CAS で**落とす: `transition_disposition` の rehydrate / `supersede_and_activate` の
      existing recovery promotion / `open_next_generation`。R4 review (j#94707 R4-F1) は、この
      うち v9 field だけが 3 経路とも clear 対象から漏れていたことを検出した。
    - **read gate は完了した generation の observation しか返さない**。`process_release` が
      `released` でない row は、caller が何を検査しているかに関わらず component 境界で拒否する。
      拒否理由は release 軸の状態ごとに**明示分岐**する (畳むと consumer が retry 可能な状態 /
      invariant 違反 / 判定不能を区別できない。j#94727 R5-F1 / j#94738 R6-F1)。
      - **この gate が分類規則を持たない state** →
        **`release_observation_release_state_unknown`**。`process_release` は
        `TEXT NOT NULL` で **CHECK 制約が無く**、row decoder も文字列を pass-through するので、
        legacy / corrupted / 手編集 row は任意の値を持ちうる。これは **outcome-unknown** であり、
        hibernate rail の既存 ruling (`release_state_unknown`、j#86776 R5-F5 / j#87226) と同じ扱い =
        **決定的な分類へ畳まない**。3 つの規律がある (j#94750 R7-F1 / R7-F2)。
        - 判定は **byte-exact** で行い trim しない
          (`"released "` は normalize すると canonical になり、R7 以前は **proof として通っていた**。
          closed vocabulary は格納値のまま比較する — `lane_kind` の j#85852 F1 と同じ規律)。
        - 判定は **`RELEASE_STATES` への membership ではなく、gate 自身が規則を持つ token 集合**で
          行う。R7 以前は「知っている state を拒否し、残りは `released` として fall-through」だった
          ため、**vocabulary に第五 member を足すと proof を返した** (実測)。
          **accepted-set への membership は、各 member に規則があることを保証しない。**
        - state の判定は **observation の decode より前**に行う。R7 以前は decode が先だったので、
          同じ unknown token が observation の形 (empty / malformed) に応じて `absent` /
          `unreadable` と報告され、**state 固有の診断が別 field の形に乗っ取られていた**。
      - survivor fence (`released_locator_verdict`) も **raw exact state を先に分類**し、
        non-canonical はすべて同じ typed unknown を surface する (綴りで detail が変わらない)。
        canonical な非 `released` state は従来どおり generic な `release_evidence_absent` を保つ。
      - 同じ `process_release` を読む **hibernate supervisor enumeration も byte-exact** に分類する。
        R7 以前は `.strip()` していたため `"released "` が terminal として消え、
        **`" not_requested"` が redrive (actuation を伴う) path へ admit されていた** (j#94750 R7-F3)。
      - **release driver も 4 canonical state の explicit rule** にする。R8 以前は `else` が
        `released` を意味していたため、未分類の格納 state が **pane を 1 つも閉じずに
        `released` を返し、hibernate の clean fully-actuated success として公開されていた**
        (j#94778 R8-F1)。未分類値は **raw state を保持した typed refusal** (`release_state_unknown`)
        とし、`HibernateOutcome.is_success` の completed-release 条件を満たさないようにする。
      - **CAS policy predicate も byte-exact**。R8 以前は `release_transition_allowed()` /
        `rehydrate_allowed()` が格納値を `norm()` していたため、padded `not_requested ` からの
        `request_release` が **applied=True** になり、**格納値が canonical `requested` へ
        書き換えられていた** (j#94778 R8-F2)。invalid な格納値が **canonical authority へ洗浄され**、
        以後の byte-exact reader はその row を正当な canonical row として正しく信用してしまう。
        read 側だけを exact にしても writer が塞がっていなければ意味がない。
    - **原則: ingress は正規化してよい / 格納 authority の判定は正規化してはならない**
      (j#94778)。caller から受け取る引数は trim してよいが、**row が既に保持している値**の権威判定を
      trim すると、storage が持っていない canonical な事実を捏造することになる。
      この原則は release 軸に限らない。**`replacement_state` 軸でも同型の実 write laundering を実測**
      した (j#94805 R9-F3): padded `" not_requested"` から replacement generation が開き、
      padded `"requested "` が `pending` へ進み、padded `"replaced "` が settled gate を通って
      lane を `active` へ戻していた。`replacement_transition_allowed` /
      `replacement_open_allowed` / `replacement_settled` / `disposition_transition_allowed` は
      いずれも byte-exact。disposition については **実 CAS は先行する exact expected-state guard で
      既に `unexpected_state` / zero-write 拒否**であり、pure predicate の契約整合と将来 reuse 対策
      として修正した (実 laundering は再現していない)。
    - **domain の非成功判定は public 境界の exit code と一致させる** (j#94805 R9-F1)。
      `HibernateOutcome.is_success` が非成功と判定する executed outcome は、CLI も非 0 で終わる。
      literal token を CLI 側で列挙すると domain から drift する (unknown / `requested` が exit 0 に
      なっていた)。preflight-only は `executed=False` なので従来どおり exit 0。
    - **格納 raw 値の text 表示は escape する / domain は raw を保持する** (j#94805 R9-F2)。
      未分類 state をそのまま text へ埋めると **改行で偽の行を作れ、ANSI ESC も通る**ことを実測した。
      canonical token はそのまま表示し、それ以外は可逆な quoted 表現で出す
      (`render_release_state`)。JSON payload は raw を保持する (encoder が escape するため安全)。
    - **同じ規律は `binding_kind` にも適用する** (j#94840 R10-F1)。census は
      `norm(...binding_kind)` **19 site / 14 module**で、`declare_lane` の
      `kind = norm(binding_kind)` **1 件だけが ingress**、残る **18 が stored-read** だった。
      実測: 格納 `"issue "` の row が `backfill_active_binding` (revision 1→2) と
      `open_next_generation` (generation 1→2) を通り、`record_matches_binding` の issue branch は
      **`binding_kind` を一切見ずに** `project_gateway` row を issue binding として match していた。
      18 site を共有述語 `stored_binding_kind_is()` (byte-exact) へ寄せ、issue branch に
      canonical kind 要求を追加した。**byte-exact だけでは不十分**な site もある:
      `open_next_generation` は分岐がどれも成立せず **fall-through して write に到達**していたので、
      vocabulary 外の kind を **outright 拒否**する guard を足した (j#94750 R7-F1 と同じ形)。
      **空文字も non-canonical** である (j#94992 R11-F1)。R11 では decoder の
      `str(row[17] or BINDING_KIND_ISSUE)` を「pre-v5 legacy default の codec」と説明したが、
      **これは実 migration と一致しない誤りだったので撤回する**: v4 row は `binding_kind` 列
      **自体を持たず**、migration は `ADD COLUMN binding_kind TEXT NOT NULL DEFAULT 'issue'` で
      **literal token を backfill** する (SQLite の ALTER 挙動を単独で実測済み)。したがって
      **genuinely migrated row は `'issue'` を持ち、空文字は legacy 産物ではない**。
      decoder は raw bytes を保持し、空文字は他の non-canonical 値と同じく fail-closed にする
      (実測: 修正前は raw `''` が canonical へ昇格して `open_next_generation` が
      **generation 1→2 で applied**、修正後は `unexpected_state` / zero-write)。
    - 2 つの述語は責務が異なるので混同しない: `is_canonical_release_state()` =
      **vocabulary に属する値か** (fence / hibernate enumeration / driver / CAS policy) /
      `_CLASSIFIED_RELEASE_STATES` = **read gate が規則を持つ値か** (第五 member を proof へ
      通さないため意図的に別集合。j#94750 R7-F1)。
      - `requested` / `partial` → **`release_observation_generation_not_completed`**。
        observation は**現 generation のもの**である (`record_release_outcome` は
        `process_release` のみを進め observation を書き換えない)。未完了なだけであり、
        `partial` はまだ slot を閉じうるので survivor proof にはならない。
      - `not_requested` かつ observation が残存 → **`release_observation_stale_after_reset`**。
        canonical `not_requested` **のみ**に予約する。
        reset writer が clear するのでこの shape は到達不能であり、**reset invariant 違反**
        (旧 build / 手編集 / reset を伴わない writer 追加) を名指しする。
      reset writer が正しく clear していれば後者は生じないが、「caller が到達しないから残しても
      安全」という前提は本 issue で 2 度撤回している (timestamp / caller 供給 pins) ため、
      **到達可能性の前提を安全性の根拠にしない**。
    - **live-zero でも generation を開く**。従来は「閉じる slot が無い」と early return して
      何も記録しなかったが、それでは resume が「hibernate 時 process 0」と「survivor がいて
      証拠が残らなかった」を区別できない。complete-empty を記録することでこれを検査可能な
      事実にする。**その結果 live-zero hibernate の `process_release` は `released` へ到達する**
      (従来 `not_requested`)。coordinator 承認: #14477 j#94596 item 1。
    - **`released` は release command / generation の完了であり、process absence の正本ではない**
      (j#94596 item 1)。actuation 0 件でも矛盾しない。**consumer は `released` 単独を deadness /
      empty の証明へ昇格してはならない**。liveness の正本は引き続き live inventory である。
      retire / reconcile の各 surface は `released` に加えて既存の fresh live-zero / foreign /
      multiplicity / binding / revision fence を **連言し続ける**こと。complete-empty でそれらを
      short-circuit しない (j#94596 item 2)。
    - **CLI / JSON / docs は live-zero を事実どおり表現する** (j#94596 item 3):
      「0 slot observed・0 close actuation・release generation completed」。「process を閉じた」と
      誤読させる表現にしない。
    - **残る trust boundary**: writer が **fabricated observation** を渡す余地は残る。今日の
      「`pins=` ならどこでも暗黙に受理」から「唯一の監査可能な seam で明示的に主張」へ変わった
      が、暗号的証明ではない。clock / locator 再利用にも依存しない正しい終点 (lane epoch を
      attestation へ bind し strictly-newer を要求) は **#14756**。
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
      `ReleasePin` の後方互換 decode は維持。★**action-time の current-liveness 世代照合**
      (`ProcessGenerationPin.binds_same_generation`, #13846) は identity tuple
      (`role/provider/assigned_name/locator`) を **strict 一致**で束ね、`runtime_revision` は
      **どちらか一方が空なら非 discriminant**（declared は herdr runtime version 観測 surface が無く空、
      live `agent list` row は供給しうる）として扱い、**両側が観測して差異があるときだけ** re-launch
      された別世代として fail-closed する。full `match_key` 等価は「declared 空 vs live 非空」を mismatch と
      誤読し、current な fresh generation を `worker_liveness_authority_conflict` で拒否していた
      (#13846)。これは #13845 の「CAS を共有 predicate へ一般化しない (「空 or 一致」)」警告が対象とする
      **row-shape CAS write** の共有述語一般化ではなく、上で optional evidence と定義済みの
      `runtime_revision` を **read-time liveness 照合**で
      その定義どおり非 identity として扱うだけであり、identity 4 field には空許容を持ち込まない。
      ★★**action-time 世代 authority の 2 source** (#13846 R4、installed 実機証拠 #14062 j#82028):
      worker dispatch admission の `generation_binding_current` は **どの宣言 surface が authority を
      供給したか**で 2 経路を持つ。**(a) declared worker pin 経路** (adopt / hibernate-repair が
      `declared_slots` を書いた row): 上記 `binds_same_generation` で live pin と束ね、かつ startup
      self-attestation を declared locator に照合する (最強 — locator drift / both-observed revision
      mismatch を fail-closed)。**(b) slot-less create 経路** (`sublane create --no-dispatch` は
      `declare_active` を通り `declared_slots` を **一切書かない** — 正当な generation-1 shape): declared
      worker pin が無いので generation authority は **live worker の startup self-attestation を LIVE
      locator に generation-bound したもの** (herdr の世代 discriminant は live locator、attestation store
      は runtime version を保存しない — `herdr-native-identity.md`) とし、assigned_name も明示照合する。
      **live row の provider / detected agent (`agent` field) が resolved worker provider と一致する
      ことも fail-closed 照合する** (#13846 R4 review F1): declared 経路の `binds_same_generation`
      (`live_pin.provider`) が持つ provider 軸を slot-less 経路でも保ち、name+locator 一致でも
      wrong-provider row を zero-send する (surface されない field は name-encoded provider に fallback)。
      R3 の `binds_same_generation` は declared pin が存在する前提だったため経路 (b) では発火せず、空
      snapshot が false `worker_liveness_authority_conflict` を生んで installed fresh E2E 全体を止めていた。
      経路 (b) は **`PIN_PAIR_ABSENT` (真に slot-less な row) のみ**に適用し、positively suspicious な
      declared shape (foreign / mixed / duplicate / incomplete / unreadable) と、live 側が absent /
      unattested / stale (locator drift) な slot-less row は従来どおり fail-closed (「row に pin がある」
      ことは current generation の証明にならない)。field 分類: **identity authority** =
      role/provider/assigned_name/locator、**generation authority** = live locator に bound した startup
      self-attestation (declared pin 存在時は locator-drift / both-observed-revision fail-closed を加える)、
      **observation metadata** = runtime_revision (both-observed かつ差異のときのみ discriminant)。
      conflict reason には **どの authority field が不一致か**の value-free token
      (`_generation_binding_detail`、locator / raw output / secret を露出しない) を付す (#13846 R4、
      finding j#82030)。
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
      ★**create path も同じ residual を持つ** (#14475、live blocker #14462 j#88463)。`sublane supersede` の handover は
      recovery lane の `active` 行を **自ら** generation 1 で INSERT し、`worktree_identity` / `declared_slots` を
      意図的に空のまま残す (「その lane が実際に create されたときに書く」)。ところが後続 create の
      `LaneLifecycleStore.declare_active` は既存 row を見て `already_declared` で zero-write 拒否し、
      `declare_created_lane_lifecycle` はその outcome を **捨てていた** ため、create が算出済みの canonical token が
      row へ届かず lane は worktree-unbound のまま稼働した (guarded recovery の launch fence で初めて露見し、
      その時点では既に destructive close 済)。create path は `already_declared` 拒否時に **同じ**
      `backfill_active_binding` CAS へ route する。`declared_slots` には **row 自身の現在の pins を再読して
      exact に渡す** (review j#88477 F1)。空集合を渡すと store の「non-empty different slot snapshot」fence に当たり、
      **pin 既着地 row (=まさに #14462 の形) では必ず zero-write** となって、「lane 自身の declaration surface を
      再実行する」という回復 runbook が成立しない action を指してしまう。pins を **不変のまま**渡せば store は
      「既に exact に存在する」と見なし、空の worktree field だけを書く (pin は決して書き換えない)。read→CAS の
      窓は `expected_revision` が覆う: pins を動かした並行 write は revision も動かすので CAS は `stale_revision` で
      拒否し、stale snapshot を書き戻さない。pins が decode 不能な row は触らない (fail-closed、推測で再 encode しない)。
      token 無しの create は binding を推測しない。
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
      ★**foreign / duplicate / unreadable occupant gate**(#13897、first observed #13845 j#80123):
      `expected_live_slots` は **managed role のみ**を集計するため、`live_pair_present` の live-zero
      read だけでは「expected slot が live でない」しか言えず「unit が quiescent」を言えない。foreign
      provider **だけ**が占有する unit は live 0 と測定され、これを quiescent と誤認すると実 process
      稼働中の legacy row を terminal `retired` に確定してしまう (実測: foreign-only inventory で
      `exit 0` / `state=retired`)。修正は #13845 bound-retire と同じ共有 primitive(`expected_slot_rows`
      raw scan / `classify_named_slot`)で raw scan を対にし、**exact managed-slot absence を必須のまま**
      連言で 3 事実を各個 gate する: target unit の foreign/unexpected occupant → `foreign_inventory_present`、
      同一 canonical slot の重複 row → `duplicate_inventory`、locator 無し expected row を liveness contract が
      dead と断じない → `expected_identity_unresolved`。いずれも zero-write。present-but-blank residue
      (`classify_named_slot`==`SLOT_STALE`) は positive proof of deadness ゆえ block しない。fence は target
      unit scope(別 lane occupant / default-lane coordinator pair は foreign 扱いしない)。idempotent replay も
      同 gate を通過してから success(persisted `retired` + foreign 稼働 → success withhold)。#13842 reconcile /
      #13845 bound-retire / #13754 guarded close は非退行。
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
    - **hibernated bound live-zero terminal retire** (`LaneBoundRetireStore.retire_released_hibernated_bound`,
      #13845、live evidence #13810 j#79416)。#13841 / #13842 が **どちらも拒否**する側の隙間: **hibernated /
      released** かつ `worktree_identity` が **非空**（#13754 / #13809 / #13810 で binding 記録済みの **bound**
      row）で、live pair は **既に消滅**しているケース。3 契約の間に収束経路が無く恒久停止する: #13754 guarded
      close は binding を attest した後 close 対象 0 を観測するが、zero-close が retire と認められるのは durable row が
      **既に** `retired` の時だけ（#13754 zero-close fence）ゆえ `zero_close_unproven` / `closed=[]` /
      `durable_retirement=""` を恒久的に返す。#13841 migration は `worktree_identity` **空**（legacy signature）を、
      #13842 reconcile は worktree/`declared_slots` **空 かつ live pair 有り**を要求するので、bound row は双方で
      `CAS_UNEXPECTED_STATE` zero-write。new pair を起動して再退役するのは不要な actuation ゆえ採らず、この surface は
      該当 row を **直接** #13689 terminal `retired` へ 1 本の bounded CAS で移す — **metadata only**、process
      launch/close/resume も worktree/branch 削除も伴わない。public high-level path
      (`sublane retire --retire-hibernated-bound`) が **連言検証**する: exact issue+lane+workspace・**bound worktree
      一致**（#13754 `attest_retire_target` を再利用。空 binding は `worktree_binding_unverified` で #13841 へ route、
      別 token は `worktree_binding_mismatch`）・`--worktree` 実 branch==`--branch`・integration ancestry・
      **readable live inventory の全 expected slot absent**・**target unit に foreign/unexpected occupant 無し**・
      exact lifecycle revision。live 残存 / inventory unreadable / foreign occupant / branch mismatch / detached /
      unintegrated / revision race / pending replacement はすべて zero-write。★**live-zero と unoccupied は別の事実**
      (review j#80115 F1): `expected_live_slots` は **managed role のみ**を集計するため、foreign provider だけが
      占有する unit は「live 0」と測定される。これを quiescent と誤認すると、実 process が稼働中の lane を
      terminal `retired` として記録してしまう (実測: foreign-only inventory で `exit 0` / `state=retired` /
      `expected_live=[]` / durable `retired`)。したがって `expected_live_slots` の live-zero に加えて
      **`plan.foreign_names` 非空を独立に fail-closed** する (`foreign_inventory_present`)。この surface は
      何も close しないので unit を空にする手段を持たず、occupant は触れてはならない foreign process である。
      同 class を #13842 は `foreign_at_position` で gate 済みであり、その正解形を踏襲する。verdict は
      `expected_live` と `foreign_names` を **別 field** で surface し、どちらの測定が拒否したかを operator が
      識別できるようにする。foreign fence は idempotent `already_retired` の **前**に置く: persisted `retired` は
      現在の quiescence を証明しない (j#79150 F2 と同型)。★**`expected_live_slots` は aggregate であり 3 つの事実を
      落とす** (review j#80148): (a) unexpected occupant、(b) **duplicate multiplicity** (role が set へ集約される)、
      (c) **locator 無し row** (無条件 skip)。ゆえに空の結果は「expected role が live でない」であって「unit が空」
      ではない。本 surface の契約は後者の強い命題を要するので、raw scan `expected_slot_rows` を併読して各軸を
      fail-closed する: **duplicate** (**同一 canonical slot** の row が 2 件以上 → `duplicate_inventory`。herdr assigned
      name は一意ゆえ ambiguity であり、`herdr_target_resolution` も `multiple_matches` で送信を拒否する。live check
      の **前**に置く — locator 付き duplicate を `live_pair_present` と呼ぶのは問題の名指しとして誤り)、
      **locator 欠落** (`expected_identity_unresolved`)。★**duplicate の key は role ではなく decoded
      `(workspace_id, lane_id, role)`** (= canonical assigned name と 1 対 1、review j#80187 R3-F1): shared slot
      `(project workspace, lane, role)` と legacy twin slot `(worktree token, default, role)` は **role を共有するが
      別 slot** であり、共存は `test_legacy_twin_closes_alongside_shared_unit` が pin する正規の互換挙動である。
      role で集約すると、この通常形を uniqueness 違反と誤読し、しかも locator-less stale twin は close 対象でもない
      ため **恒久 terminalize 不能**を作る (= #13845 の defect を別 shape で再生産)。`unresolved` 判定も
      **candidate ごと**に行い、role は表示用の集約にとどめる。★**進行の条件は deadness の positive な証明であって
      liveness の証明の不在ではない**: `classify_named_slot` は "conservative in the never-clobber direction" で
      minimal row を **live** と読むため、同 contract が `SLOT_STALE` と **積極的に判定した** row (detected agent が
      present かつ blank / status が present かつ unknown) のみを residue として通す。全 residue を block すると
      #13845 が解消しようとしている「恒久 stuck」を別 shape で再生産するため。走査 helper `expected_slot_rows` は
      additive で `expected_live_slots` を behavior-preserving に再定義したもの (**guard の共有ではなく走査の共有**。
      判定は各 surface が持つ)。書込は「row 存在 かつ exact `expected_revision` 一致」かつ「`hibernated` /
      `binding_kind='issue'` / この exact issue 所有 / project scope 無し / `worktree_identity` **非空 かつ caller の
      attested token と一致**」かつ「`process_release='released'` / replacement settled」の全成立時のみ。
      ★**worktree token は CAS 内（row lock 下）で再照合**する: action-time attestation は診断であって authority では
      ない。★**declared pins / worktree identity / generation / release / replacement は保持**（#13845 acceptance）—
      書くのは disposition + decision anchor + revision のみ。`reconcile_phase` も **空のまま**で、これが ordinary
      terminal retire と #13842 reconcile-owed close を区別する正本であり続ける（review j#79320 R4 の
      collision-proof 不変を維持）。★duplicate replay の冪等性は維持するが **success を返す前に live-inventory zero を
      action-time 再確認**する（#13841 review j#79150 F2 と同じ不変）: persisted `retired` は現在の非稼働性を証明しない。
      ★`transition_disposition` の generic edge を使わない理由は #13841 と同じ（release proof と bound-worktree
      signature が **guard の一部**）。★#13841 / #13842 の CAS を共有 predicate へ一般化 **しない**: それぞれの guard は
      自 ticket の evidence に対して review された安全契約であり、「空 or 一致」のような共有述語は sibling surface が
      拒否するために存在する shape を 1 edit で通してしまう。各 surface が自らの signature を literal に述べ、他は
      zero-write で拒否する。`--retire-hibernated-bound` は `--execute` / `--migrate-hibernated-legacy` /
      `--reconcile-hibernated-live` と競合する destructive intent ゆえ **2 つ以上の同時指定は command-time zero-write
      error**。
    - **active live-zero terminal retire** (`LaneActiveRetireStore.retire_active_live_zero`,
      #14242、live evidence #14222 j#85208-j#85209)。#13754 / #13841 / #13842 / #13845 が **いずれも
      収束させられない**隙間: **active** かつ `binding_kind='issue'` かつ `worktree_identity` **非空**（bound）で、
      issue と children が closed・head integrated・worktree clean なのに managed pair が **既に positive zero**
      のケース。#13754 guarded close は閉じる対象が無く `zero_close_unproven` を恒久的に返し（row が未 `retired`
      ゆえ zero-close を retire と読まないのは正しい fail-closed）、#13845 は CAS が `hibernated` +
      `process_release='released'` を要求するため `not_hibernated_bound_state` で拒否、#13841 / #13842 は
      `worktree_identity` **空**を要求する。本 surface は 1 本の bounded CAS で `active -> retired` へ
      **metadata-only** に terminalize する（process launch/close/resume なし、worktree/branch 削除なし）。
      ★**#13845 と決定的に異なる点: release witness が存在しない**。#13845 は live-zero read と durable
      `process_release='released'` という**独立 2 witness**を連言するが、active row は構造上
      `process_release='not_requested'`（`request_release` 自体が active row を `unexpected_state` で拒否する）。
      したがって **live-inventory zero read が唯一の liveness authority** となり、caller 側の要求水準が上がる:
      unreadable inventory / duplicate canonical slot / locator-less（liveness contract が positive stale と
      言わない）row / foreign occupant は **すべて zero-write 拒否**し、かつ **zero read を測定した exact revision**
      を CAS へ渡す（read と write の間に relaunch された pair は `CAS_STALE_REVISION` で敗れ、clobber しない）。
      ★書込条件は「row 存在 かつ exact `expected_revision` 一致」かつ「`active` / `binding_kind='issue'` /
      この exact issue 所有 / project scope 無し / `worktree_identity` 非空かつ attested token と一致」かつ
      「`process_release ∈ {not_requested, released}` / replacement settled」の全成立時のみ。`requested` /
      `partial` は actuator が pane を閉じている最中でありうるため拒否（active row では到達不能だが backstop として
      保持し、white-box test で固定）。★worktree token は CAS 内（row lock 下）で再照合する — attestation pre-gate は
      **診断**であって authority ではない（pre-gate を外しても CAS が `not_active_bound_state` で拒否することを
      test で固定）。★declared pins / worktree identity / generation / release / replacement / `reconcile_phase` は
      **保持**し、書くのは disposition + decision anchor + revision のみ。★duplicate replay は idempotent だが
      success を返す前に live-zero を action-time 再確認する。★**#13845 の CAS を `active` 受理へ広げない**:
      広げると active row が「自らは決して供給できない release proof」の上で terminalize しうる。各 surface が
      自らの signature を literal に述べる原則どおり、専用 store を追加した。`--retire-active-live-zero` は
      `--execute` / `--migrate-hibernated-legacy` / `--reconcile-hibernated-live` / `--retire-hibernated-bound`
      と競合する destructive intent ゆえ **2 つ以上の同時指定は command-time zero-write error**。
      ★**launch / terminalize 排他**（#14242 review j#85219 F1 → design answer j#85269）: revision
      fence は lifecycle row の mutation しか捕捉せず、**process launch は同 row を触らない**
      （`declare_active`(existing)=`already_declared` zero-write、`declare_lane`=idempotent、実測）
      ため、live-zero read → terminal CAS の間に pair が起動すると live のまま `retired` になりえた。
      新 schema / durable claim は導入せず、**#13882 three-boundary lock を再利用**して解決する:
      全 managed launch（ordinary create/heal、v1 replacement binding、quarantine `heal_receiver`、
      lane identity を持たない bare / scratch / shared-space session start）は既に home の
      attestation-store lock を **shared 非 blocking** で「最初の attestation read 前から最後の
      actuation まで」保持するので、terminalizer が同 lock を **exclusive 非 blocking** で
      action-time 半分（lifecycle 再読・inventory 再読・全 gate・terminal CAS）の間保持すれば
      reader-writer 排他が成立する。launch 先行 → terminalize は `launch_in_flight` で zero-write /
      terminalize 先行 → launch は admission で失敗し **workspace/tab/agent 生成前に zero-spawn**。
      holder crash は OS が lock を解放するので stale claim / TTL / takeover recovery は不要。
      lock 不可用（`fcntl` 非対応）は `exclusion_unavailable` で fail-closed（無施錠で進めない）。
      ★residual（lock 正本が既に明記、本件で解決したと主張しない）: 本 protocol を知らない別 vintage
      の launcher は排除できない。durable claim を足しても旧 binary は field を読まないため同じ。
    - **hibernated bound declared-pin repair** (`LanePinRepairStore.repair_hibernated_bound_pins`,
      #13879、live evidence #13846 j#79915)。#13809 / #13841 / #13842 / #13845 が **いずれも拒否**する隙間:
      **hibernated / released** かつ `worktree_identity` **非空**（bound）かつ `declared_slots` が **空**
      （pins-only gap）で、exact managed pair が **live** に観測されるケース。収束経路が無い: #13809 backfill は
      **active** row 専用、#13841 / #13842 は `worktree_identity` **空**を要求、#13845 は同じ bound signature だが
      **live-zero** 側を対象とし terminalize する。結果 `sublane recover-pair` (#13847) が declared pins 必須ゆえ
      `hibernated_record_missing_pins` を恒久的に返す。この surface は **空の `declared_slots` のみ**を
      1 本の bounded CAS で充填する — **metadata only**、process launch/close/resume/send も worktree/branch 削除も
      伴わない。★**#13847 の declared-pins 前提は緩めない**: 本 surface はその前提が読む metadata を repair するだけで、
      recover-pair の **preflight 判断 (どの slot が recoverable か / 何を close するか) は不変**（境界は実装と doc の
      双方で明示）。★ただし #13920 が recover-pair の **pin 解決**を `### declared pins の role 語彙` の owner 境界へ
      移し、legacy 綴りの read-compat と blocked reason の細分化 (`hibernated_record_missing_pins:<reason>`) を
      加えた。これは「非空 pins を有効な証明として扱わない」方向の**強化**であり、緩和ではない: repair が書く
      canonical snapshot の扱いは変わらず、repair 後に recover-pair が解決できるという本 surface の目的も変わらない。
      public high-level path
      (`sublane repair-pins`、default は preflight・`--execute` で CAS) が **連言検証**する: exact
      issue+lane+workspace、bound worktree token 一致、そして **live pair の全軸**は #13842 の pure
      `decide_pair_reconcile` を **無改変で再利用**（present / unique / live / idle-or-turn-ended /
      composer-settled / locator-bound attested / 別 locator / foreign 無し / inventory readable）。
      ★**guard の共有ではなく走査の共有**の原則どおり、#13842 の観測 scan は `observe_pair` として公開し
      （事実収集のみ）、**row-shape CAS は各 surface が literal に自 signature を述べる**（#13845 j#80187 の規律）。
      #13842 の signature（worktree **空**）と本 surface（worktree **非空 かつ一致**）は **構成上排他**であり、
      同一 row が双方の対象になることはない。書込は「row 存在 かつ exact `expected_revision` 一致 かつ **exact
      `expected_generation` 一致**」かつ「`hibernated` / `binding_kind='issue'` / この exact issue 所有 /
      project scope 無し / `worktree_identity` **非空 かつ token 一致**」かつ「`process_release='released'` /
      replacement settled」の全成立時のみ。★**generation を revision と併せて guard** する: pins は観測した
      process generation を名指すので、観測後に再 incarnate した row の空 snapshot は **この pair のものではない**。
      ★worktree token は CAS 内（row lock 下）で再照合（action-time 観測は診断、CAS が authority）。
      ★**replay は byte-equal のみ idempotent**（#13879 acceptance 4）: 完全一致は revision を上げない no-op success、
      **non-empty かつ異なる** snapshot（recycled generation / foreign pin set）は `already_declared` zero-write —
      既存 snapshot は決して上書きしない。空の pin set は caller error（何も証明しない repair）。
      ★**default preflight は `--execute` の結果を全軸で予告する**（review j#80547 F1）。base signature は
      `declared_slots` を **意図的に未照合**にする（byte-equal replay は正当に non-empty snapshot を見るので
      「空」は前提条件ではない）が、その結果 preflight 分岐が persisted snapshot を見ずに一律 `repairable` /
      exit 0 を返すと、**divergent row を exit 0 と予告しながら `--execute` は `declared_pins_divergent` で
      拒否する**契約分裂を作る（byte-equal row も「repair する」と予告して実際は無書込）。zero-write ではあるが
      public default が acceptance 4 と矛盾する green signal を operator / automation に返すため defect。
      preflight は observed pins を **CAS と同一の `validate_declared_slots` → `encode_declared_slots` 経路**で
      encode して persisted と byte 比較し、空→`repairable` / byte-equal→`already_repaired` / non-empty 相違→
      `declared_pins_divergent` fail-closed を返す（同一経路でないと「preflight は byte-equal、CAS は divergent」
      という新たな分裂を作る）。この比較は **preflight 分岐に限定**し `--execute` は常に CAS へ到達させる —
      診断が authority を先取りすると、read と CAS の間で row が変化した際に stale な診断が
      authority の受理する row を拒否しうる。★test は各 state を個別に assert するのでは足りず、
      **preflight と execute の一致（prediction）を性質として pin** する: byte-equal shape では
      誤 `repairable` と正 `already_repaired` が `ok` / `reason` を共有するため、**state を比較しないと
      同 defect を取り逃す**。
      ★**書くのは `declared_slots` + decision anchor + revision のみ**。disposition / generation / worktree /
      release / replacement / `reconcile_phase` は **保持**（`reconcile_phase` は空のままで、#13842
      reconcile-owed close との区別を維持）。★**pin の `role` は消費者と同一語彙**でなければならない:
      canonical vocabulary の**唯一の owner は `core/state/lane_pin_role.py`**（#13920）であり、pin を書く /
      読む surface は例外なく `PIN_ROLE_GATEWAY='gateway'` / `PIN_ROLE_WORKER='worker'` をそこから import する
      （`### declared pins の role 語彙` を読む）。`domain/sublane_lifecycle` は **同名で値が provider**
      (`'codex'` / `'claude'`) の定数を export するので、後者で pin すると consumer は role 解決に失敗し、
      repair が「成功」しても `hibernated_record_missing_pins` が残る（= #13879 / #13920 の defect が生存する）。
      `attested_at` は検証済み startup self-attestation の
      `observed_at`（実証拠）、`runtime_revision` は空（herdr に runtime version 観測 surface が無い、
      #13809 / #13810 R4-F1 — fabricate しない）。startup self-attestation は generation ごとに 1 回・locator で
      pin されるので、同 generation の replay は同じ `observed_at` を読み byte-equality が安定する。
    - **hibernated bound stale-pair convergence** (`sublane converge-bound-pair`, #13933)。
      上記 `repair-pins` が意図的に拒否する **stale / unattested pair** と、`recover-pair` が意図的に要求する
      **declared pins** が同時に欠落した場合、互いが相手の前提になり循環する。専用 rail はこの交差 shape
      （`hibernated` / `released` / issue-bound / exact non-empty worktree binding / `declared_slots` empty）だけを扱い、
      sibling surface の authority を緩めない。
      - default は read-only preflight。exact issue / lane / lifecycle revision+generation / resolved worktree token+
        branch / action-time slot set を digest した structured direct-owner marker を出す。`--execute` は
        credential-gated `LiveRedmineJournalSource` で **その exact journal を fresh read**し、prose でなく marker の
        byte-exact field 一致だけを authority とする。
      - replacement は `ReplacementTransactionStore` の immutable manifest と
        `ReplacementActuatorUseCase.drive_worker_recovery` を使う。対象は positive-fact classifier が
        `recover_bad_generation` とした exact locator のみ。inventory unreadable / duplicate / foreign / busy /
        pending composer / dirty-or-unreadable worktree / branch mismatch / revision race は transaction plan・close 前に
        fail-closed。transaction plan 直前に full observation を取り直してdomain decisionへ再投入し、初回writeは
        owner markerが束縛したinitial snapshotと完全一致する場合だけ許す。既存immutable transactionのretryは
        `plan_transaction` を再実行しない。さらにclose直前にも既存 `identity_observation_for` で participantの
        lifecycle revision/generationとslot identityを再照合し、hibernated/released/bound/settled signatureおよび
        clean exact branchを再確認する。partial close の absent slot は **同じ transaction participant がclose済みと
        証明する場合だけ** replayでき、名前やcacheからabsent proofを捏造しない。
      - fresh launch は replacement `action_id` を startup self-attestationへ binding する。最終 pair は両 slot が
        unique / live / idle / composer-settled / locator-bound attested で、replacement participant はさらに exact
        action-bound でなければ pins を作らない。pins はこの最終 live pairだけから構築し、
        `LanePinRepairStore.repair_hibernated_bound_pins` の revision+generation+worktree CASへ渡す。
      - pin CAS 後は replacement transaction を `completed` にして lease を releaseするが、lane disposition は
        **hibernated のまま**。resume / redispatch / callback send は行わない。次 action は既存 public hibernate /
        retire/recovery rail が所有する。replay は exact transaction + byte-equal pins のみ idempotentで、他 lane / default
        coordinator / worktree / branch を変更しない。
    - **hibernated bound pending-composer preparation** (`sublane prepare-bound-pair`, #13933)。
      上記 convergence は pending composer を常に preserve する。この hard block は緩めず、live dogfood で確認した
      `hibernated` / `released` / issue-bound / worktree-bound / pins-empty pair の pending generation だけを事前に
      action-bound relaunchする別 rail。default preflight は exact lifecycle revision+generation、resolved worktree identity+
      branch、full pair slot digest、discard role set を束縛する structured marker を返す。`--execute` は
      `LiveRedmineJournalSource` で exact journal を fresh readし、`direct_owner` marker の byte-exact 一致を要求する。
      - 対象 role は positive-fact classifier が `preserve_pending_composer` とした slotのうち、exact assigned name+
        locatorが一意、non-productive、composer readable、delivery ledgerと相関する markerが無い generationだけ。
        correlated / ambiguous / unreadable input、busy provider / tool-child、foreign / duplicate / newer generationは
        approvalがあっても zero-close。
      - active lane用 `sublane quarantine` の `lane_active` guardは緩めない。本 rail は hibernated-bound signatureを
        lifecycle revision+generation、released state、empty declared pins、clean exact branchとともに毎回再検証し、
        `ReplacementTransactionStore` の別immutable actionで扱う。close直前にも同じ slot/composer/lifecycle/worktree
        authorityを再読する。pending-preservation overrideはこのactionのapproved participantだけで、generic overrideではない。
      - partial retryでold slot absentを受理できるのは、同じimmutable transaction participantがclose済みを証明する
        場合だけ。fresh launchはaction-bound attestation必須。
      - ★**action自身のcloseがpair fenceを壊す点をfenceする** (#13933 R6、live evidence #13846 j#80933 / j#80934)。
        pending composer classifierが継承する pair fence は **両 provider の live rowが揃うこと**を要求するため、
        片roleをcloseした直後にlaunchが失敗すると、**このaction自身の効果**によって残りrole が `generation_mismatch`
        となり、preflightは `no_exact_uncorrelated_pending_composer`、`--execute` は `transaction_conflict` で
        恒久停止する (gateway missing + old worker remains)。sibling の不在を再admitできるのは、**同じimmutable
        transactionがそのcloseを証明する場合だけ** (`close_proven` = live row無し **かつ** participantが `close_owed`
        を通過済み)。復帰したsibling / 証明の無い不在 / pair全体がaction-closed は継承fenceに戻し zero-close。
        live rowのidentity/uniqueness/revision/cwd判定は継承側が所有し、本railは緩めない。
      - preflightは **transaction-blindな分類がblockした場合に限り**、callerが渡したapproval journalを anchorに
        exact action idで再observeする。projectionが raw observationと同一なら「このpairを所有するactionは無い」
        として元のblockを維持する。approval不在 / credential失敗 / projection不成立は resume しない。resumeを
        報告しても新しいmarkerはmintせず、既存approvalに束縛したまま `--execute` が全fenceを再検証する。完了後もlaneはhibernatedのまま、declared pins / disposition /
        worktree / branchを変更せず、resume / dispatch / callback sendを行わない。次に通常の
        `sublane converge-bound-pair`を新しいaction-time markerで実行し、fresh pair proofからだけpinsを修復する。
      - ★**lane identity は target root の KIND で決める。callerのcwdでは決めない** (#13933 R7、live evidence #13846 j#81024、
        design answer j#81046 Decision 1)。lane の worktree identity token には 2 系統がある: linked git worktree は
        `derive_lane_workspace_token`(`wt_`)、non-git directory scaffold lane は `derive_directory_lane_token`(`dl_`)。
        判別子は「その root 自身が git worktree か」であり、`resolved == repo_root` のような caller 相対の偶然ではない。
        後者の proxy は `repo_root` が本当に coordinator の workspace root のときだけ成立し、`--repo` / cwd が渡る public rail では
        同一 lane が **実行位置で `wt_`/`dl_` を切替える**。結果、operator が案内された「lane worktree を execution root にする」経路で
        row の identity と derive が食い違い、lifecycle-signature block が collapse した (#13846 j#81024 / #13933 j#81043)。
        derive は共有 `is_git_worktree_root`(`git -C <root> rev-parse --show-toplevel == root`) を target root で probe し、
        `lane_runtime_identity(git_worktree=...)` で family を選ぶ。**persisted row に合わせて family を選ばない**: root の kind が
        決め、食い違う row は caller が報告すべき real mismatch。
        - この derive を採るのは **read/repair surface** = `prepare-bound-pair` / `converge-bound-pair` と metadata-only
          `repair-pins`。prepare/converge は共有 `_worktree` で識別子を derive し (regression:
          `test_issue_13933_lane_identity_execution_root.py` が `is_git_worktree_root` / `lane_runtime_identity` / convergence・prepare
          両 ops の `_worktree` を real git worktree に対し駆動)、`repair-pins` は inline derive を real git worktree + real probe で
          `--repo` 非依存に固定する (`test_issue_13879_...::PinRepairExecutionRootInvarianceTests`、旧 proxy で fail することを実測)。
        - ★**create/adopt declaration writer も同じ derive を採る** (#14478、live evidence #14462 j#88632)。#13933 は read/repair 側
          だけを直したため、**identity を書く側**に `resolved == repo_root` が残っていた。active declaration self-heal の runbook は
          「lane worktree を execution root にする」と案内するので、その通りに `--repo` を lane worktree へ向けると proxy が collapse し、
          **linked git worktree の lifecycle `worktree_identity` が `dl_` で backfill** される。recovery reader は同じ root から常に `wt_` を
          derive するため、直後の `sublane recover-gateway` preflight が `worktree_identity_mismatch` で close/launch/send 0 になった。
          create writer (`_record_lane_metadata` -> `_declare_lane_lifecycle`) と adopt writer (`declare_adopted_owner_row`) は
          共有 `declared_worktree_identity(worktree_path, lane_label)` を通し、**caller anchor を引数に取らない** (branch を消すのではなく、
          anchor を signature から外して分岐を表現不能にする)。regression:
          `test_issue_14478_linked_worktree_declaration_identity.py` が real git worktree / real probe / real isolated-home store に対し、
          両 anchor からの declared token、両 recovery evaluator (`LiveStaleWorkerRecoveryOps.lane_authority_reason` =
          recover-gateway / recover-stale、`lane_checkout_authority.worktree_binding_reason` = recover-pair) との join、
          non-git `dl_` 契約、divergent binding 非上書きを駆動する。recovery reader 側は**変更しない** — 既に anchor 非依存に `wt_` を
          derive しており、緩めれば #14475 の fence を弱める。
        - **destructive retire 系** (`sublane retire` guarded close / hibernated-bound / hibernated-legacy / live-reconcile) は
          この derive を採らない。#13754 は「lane worktree を `--repo` と `--worktree` の両方に渡すと token が collapse し、
          worktree-binding attestation が **意図的に fail-closed** する」ことを安全弁として持つ (false block は安全、false close が
          防ぐべき欠陥)。identity derive と authority が entangle しているため、git-kind derive への切替は destructive 挙動を
          変える per-surface 設計判断であり、別 review を要する。**共有 probe は generic authority guard へ統合しない** (design
          answer j#81046 Decision 4): 各 surface が identity family と fail-closed 条件を自分で決める。#14478 でも本 carve-out は
          維持した。
        - ★**#14478 以前に `dl_` で mis-bind された既存 live row に、in-place で rebind する public rail は無い** (review j#88645 F1)。
          measured なのは **identity axis 1 本だけ**である: `--repo` と `--worktree` の双方を lane worktree に向けると
          destructive retire は row と同じ `dl_` を derive するため `attest_retire_target` が `attested=True` を返し、
          primary-checkout anchor では `worktree_binding_mismatch` になる。**これを収束経路と読んではならない。**
          `sublane retire` は `decide_retire_integration` の **hard invariant `issue_closed` を含む** 全 invariant
          (`target_identity_known` / `issue_closed` / `callbacks_drained` / `durable_record_recorded` / `verification_passed` /
          `latest_generation_admissible`) を無条件に要求し、これらは policy field で無効化できない (config schema に key が無い)。
          つまり retire は **issue close 後の terminal cleanup** であって rebind 機構ではなく、**再 create を伴わない**。
          open issue の lane は `issue_not_closed` で block されるため、mis-bound な open row は **fail-closed のまま残る**のが
          現契約の帰結である。修正版の live 検証は、この row を handover するのではなく **canonical `wt_` を持つ fresh lane**
          (例: #14478 実装 lane) で行う。同一 issue 内で open row を handover する public rail を主張するなら、
          identity axis だけでなく **当該 surface の全 preflight を実測してから**別途記載すること (regression:
          `test_issue_14478_linked_worktree_declaration_identity.py::RetireIsNotARebindRailTests` が
          「identity が合っても open issue は `issue_not_closed` で block」「`merge_on_retire` opt-out でも invariant は緩まない」を
          pin し、全 invariant を満たした control が `retire_ok` に届くことで非空虚性を担保する) — 1 axis の green を
          rail 全体の green として書かない。
      - ★**bound-signature block は typed sub-reason で報告する** (#13933 R7、design answer j#81046 Decision 2)。
        `not_hibernated_released_bound_pins_empty` は独立 axis の連言 (hibernated / issue-bound / issue 一致 / non-project /
        worktree identity 非空 / identity 一致 / released / replacement settled / pins) であり、collapse した単一 token は
        「どの前提が破れたか」を隠す。detail は破れた axis 名のみを列挙し (raw row 値は payload に出さない)、
        resume 不成立時は `resuming=false` に typed `resume_diagnostic` (`approval_source_unreadable` / `no_matching_approval_marker` /
        `no_action_owned_progress` / `projected_still_blocked:<reason>` / `adopted`) を添えて、credential 欠落と「所有 action 無し」を
        区別可能にする。
    - **record-less scratch pair retire は本 component を書かない** (`herdr session-retire`、#13892、live evidence #13882
      j#80060 / j#80066)。`herdr session-start` の scratch pair は lane lifecycle row を **一度も持たない**ため、上記 4 契約
      (#13754 guarded close / #13841 migration / #13842 reconcile / #13845 bound retire) は **すべて row の存在を前提**に
      しており構造的に拒否する: `--execute` は `attest_retire_target` が `record is None` で `lane_owner_unverified`、
      他 3 者は `lane_not_declared` / `CAS_UNEXPECTED_STATE`。4 契約の間に収束経路が無く恒久停止する点は #13841 / #13842 /
      #13845 と同型だが、**解法は反対側**にある: row を作らない。★**row 捏造は却下** (#13882 j#80066): retire を通すためだけに
      lifecycle row を mint するのは durable authority の fabrication であり、`operator_current_state` の復旧契約
      (Redmine からの explicit re-declare) を偽の row で汚す。★**row が不要な理由は capacity 経路が live pane 由来だから**:
      `enumerate_active_lanes` は herdr live pane を畳んで roster を作り、lifecycle disposition は **非 active を除外する
      filter** としてのみ効く (`glance_snapshot_source.py`)。row を持たない unit は disposition `None` ゆえ roster に残り、
      **pane が消えること自体**が唯一かつ十分な capacity 回収手段である。★**durable outcome は `scratch_retirement_fence` の row**
      (設計 j#80526 Option A-prime)。`managed_events` は **不可** — `append_only_lossy` かつ「append 失敗は caller を壊さない」
      「completion truth にしない」charter (`:153` / `:179` / `:826`) で、F2 が要求する load-bearing な completion と正面から矛盾する。
      lifecycle row も Acceptance 4 が禁止する。ゆえに **第 4 の state kind** を立てる:
      **operational action-idempotency / side-effect transaction authority** (workflow truth でも desired-state history でも
      lifecycle authority でもない)。`managed_events` は fence completed の **後**に best-effort narrative audit として append し、
      その失敗は proven retirement を無効化しない。★signature は
      **「lifecycle record が無いこと」** で、record を持つ lane は `lane_record_present` zero-write で既存 surface へ route する
      (各 surface が自らの signature を literal に述べる規律の適用。共有 predicate へ一般化しない)。lifecycle store が **不読**の
      場合は「record 無し」と読まず fail-closed。★**attestation を要求しない** (#13892 j#80483、#13842 の意図的反転):
      #13842 が attestation を要るのは generation-bound pin を row へ **write** するためだが、scratch pair は row も generation も
      持たない。**根拠は recovery compatibility であって「session-start が attest を待たない」ことではない** (#13948 j#80989
      Documentation disposition による書き換え): #13948 以降 `session-start` は fresh launch を **fully successful と報告する前に**
      bounded startup health + locator-matched self-attestation を待つ (`herdr_startup_health.probe_session_health`)。それでも
      **wrapper 自体は non-blocking のまま**であり (`herdr_agent_attest`)、unwrapped fallback (`attest_launcher == ""`)・旧 runtime・
      launch 途中の crash・health が非 green のまま残った pair では **unattested な live pair が現に残りうる**。本 rail が回収するのは
      まさにその集合なので、attestation を close prerequisite にすれば対象そのもの (live-but-unattested = #13882 の保全 pair) を
      恒久 retire 不能にし、**over-block による恒久 stuck** (#13845 が名指しした defect 再生産) を作る。**success 条件の強化を
      retirement の over-block へ伝播させない**のが両者の分界である。identity は assigned-name の injective encoding + uniqueness
      (`session-start` が重複名で fail-closed) + foreign 不在 + duplicate 不在 + locator 一意で証明する。★**partial close は
      block せず resume** (#13847 R1-F1 と同型): 前 run で閉じた slot は positive absence として観測し残りを閉じる。#13842 の
      `pair_incomplete` block を踏襲すると中断した retire が恒久 stuck になる。acceptance 2 の「expected slot ちょうど 2」は
      **expected set (binding の gateway + worker)** の濃度であって観測 live 数ではない (観測は 2/1/0 を取りうる)。★raw scan
      (`expected_slot_rows`) を併読し duplicate は **canonical slot key** で数える (#13845 j#80148 / j#80187 R3-F1 と同じ規律)。
      ★deadness の positive 証明のみで進む: `classify_named_slot` が `SLOT_STALE` と積極判定した shell residue は agent 不在ゆえ
      turn / composer を持たず close 可 (runtime `unknown` だけで block すると residue が恒久残留する)。process mutation は
      **自 pair の pin-matched close のみ**で、worktree/branch 削除・launch/resume・store 直接 mutation は伴わない。
      ★#13918 の歴史的 owner-unbound 収束はこの signature / fence / close を再利用し、**pending composer だけ**を Redmine
      `ISSUE:JOURNAL` で locator された owner approval により明示的に破棄できる。ただし pointer syntax は authority ではない。
      credential-gated live source が exact journal を毎実行 fresh read し、structured `pending_composer_discard_approval` marker の
      `direct_owner` / `approved` / effect と exact workspace / lane / assigned-name-set digest / current role+locator digest をすべて照合する。
      missing / unreadable / wrong gate / foreign target / stale locator は reserve 前に zero-close。verified journal anchor + notes hash + target
      identity の canonical evidence は `scratch_retirement_fence` の **同じ pending/completed attempt row** に reserve 前から保存し、retry は
      fresh evidence の byte equality を要求する。したがって completion write failure 後の approval 無し・別 pointer retry は repair せず、
      `managed_events` audit failure があっても completed row から exact pointer を復元できる。approval があっても agent not idle、foreign / duplicate /
      unreadable、locator ambiguity、durable obligation、record-present は従来どおり zero-close。`issue_<id>_...` lane では approval
      issue一致と action-time Git `status` clean / current branch==lane も必須で、未保存 worktree と composer を同時に失う経路を
      作らない。non-issue scratch dogfood は Git worktree authorityを持たないためこの追加Git gateの対象外だが、exact
      workspace/lane/role/assigned-name と approval pointer は retirement audit に残す。flag無しのdefault semanticsは不変。
      ★**durable obligation は runtime signal で代替できない**（review j#80506 F4）: idle / turn-ended / composer settled は
      `ack-completion-receiver-state.md` の分類で言う **receiver state** であり「その slot に *owed* な work が無い」ことを
      証明しない。close の前に対象 assigned name 宛の **非終端 dispatch-outbox row**（`reserved` / `uncertain`）を bounded read
      （`DispatchOutboxFence.obligations_for_targets`。**全 state を causal identity 付きで返す** — `delivered` は delivery ACK であって completion ではないため store 側で捨てない）し、存在すれば `work_obligation_present`、store 不読は
      `obligation_unreadable` で zero-close。`state_of` は完全な 6-tuple `FenceKey` を要するため「これから閉じる pane に owed な物」
      を列挙できず、by-target read の新設が要った（既存 guard は不変の read-only 追加）。
      ★**close の return code は空の証明ではない**（review j#80506 F3。#13842 j#79320 R3 の precedent を自 surface へ適用）:
      close 後に **fresh inventory** で unit 全体を再測定し expected 全不在 + foreign/duplicate 不在を positive 確認できたときのみ
      durable 記録 + success。residue は `post_close_residue`、不読は `post_close_unreadable` で、**commit 済み `closed` を保持した
      まま** non-success。
      ★**zero-slot は retirement ではない**（review j#80506 F1）: absence は「pair がここに無い」ことの証明であって「**この command が
      retire した**」ことの証明ではなく、`--lane` typo と never-launched が absence として同一に見える。**exact な prior completion +
      action-time live-zero を証明できる時だけ** `already_retired`（**exit 0**）、それ以外は `retire_evidence_absent` で
      non-success / zero-write。★**その proof を持つのが専用 authority `scratch_retirement_fence`**（設計 j#80526 Option A-prime）。
      置き場の 3 択が同時に閉じていた（`managed_events` は `append_only_lossy` かつ「append 失敗は caller を壊さない」「completion
      truth にしない」charter（`:153` / `:179` / `:826`）で F2 と矛盾 / lifecycle row は Acceptance 4 が禁止 / isolated ledger は
      #13842 j#79346 R5 が撤去済み）ため、**第 4 の state kind** を立てた:
      **operational action-idempotency / side-effect transaction authority**。
      ★**#13842 の isolated-ledger anti-pattern とは別物**である: あちらは ledger loss が **live pane を抱えたままの恒久 stuck** を
      作った。本 fence の `pending` は **held / crash-released OS advisory lock 下で resume** され、`completed` の loss は success を
      捏造せず **withhold する**だけで、その時点で pane は既に消えているので **capacity leak も stuck も生じない**。残余は sibling
      authority と同じ **local total-loss indistinguishability** のみで、それ以上は主張しない。
      ★**transaction は OS advisory lock（exclusive / nonblocking）で保持**する: `BEGIN IMMEDIATE` は statement/txn の間しか持続せず
      **外部 process 操作である close を跨げない**。contention は `retirement_busy` で zero-close（待たない・奪わない）。
      ordering は reserve pending → close → fresh whole-unit remeasure → fence completed → green。`managed_events` は completed の
      **後**に best-effort narrative audit として append し、その失敗は proven retirement を無効化しない。
      ★**store identity**: artifact inventory は `lexists` semantics で DB / `-wal` / `-shm` / `-journal` / seal を見て
      **absent / present / damaged を三分**する。damaged は `retirement_authority_unavailable` で fail-closed。true first bootstrap は
      **live exact pair が positively present ∧ 全 artifact absent** の時のみ（同一 lock 下で serialize）で、**zero-slot は決して
      bootstrap しない**（失われた authority の無言再作成を防ぐ）。★**relaunch 誤認防止**: `completed` の後に新しい live slots が
      現れたら **新しい attempt（revision+1）** を開き、古い completion を稼働中 pair の proof に流用しない。
    - **session-start startup transaction** (`startup_transaction_fence.sqlite`、#13948、設計 j#80989 Q3)。
      **第 5 の state kind ではなく、第 4 の kind (operational action-idempotency / side-effect transaction authority) の
      2 つ目の住人**である。`scratch_retirement_fence` とは **store も unit も completion も共有しない**: あちらの unit/table/
      completion は *retirement* を意味し、launch rollback を同 unit の attempt として開くと **古い retirement completion が
      live な launch の proof として誤読される** (あちら自身の「relaunch 誤認防止」が禁じている形)。借りるのは pattern だけ
      — external close を跨ぐ nonblocking advisory lock / reserve-before-effect / revision+replay / artifact tri-state /
      fresh whole-unit remeasure / completion-write fail-closed。
      - **unit** = workspace + lane + requested provider set + action nonce。nonce が無いと「同じ操作者が同じ lane で
        再実行した run」が前の run の record を継承し、rollback が **自分が起動していない pane** を閉じうる。
      - **participants** = provider / assigned-name / launch locator / launch receipt。各 fresh launch の**直後**に記録する
        (最後にまとめて記録すると、2 つの start の間で死んだ run — まさに partial pair — の第 1 agent が誰の物か分からなくなる)。
      - **phases** = `planned` → `launching` → `health_check` → `rollback_owed` | `success_owed` → `completed_rolled_back` |
        `completed_success`。terminal は write-once で、replay は record から答える (再 close しない)。
      - **reserve は bootstrap 可 / rollback は不可** (意図的な非対称): reserve は *新しい* identity を作るので何も忘れない。
        absent store に対する rollback は proof を持たないため fail-closed。ここで bootstrap すると失われた authority を
        無言で再作成し、その上で pane を閉じることになる。
      - `session-start` は **debt を記録するだけで close しない** (j#80991)。destructive compensation は explicit public rail
        `herdr session-rollback --action-id <id> [--execute]` のみが行い、対象は **同 action の participant** に限る。
        embedded caller (`sublane create/start --execute`) は Herdr adapter が返す typed な action id / role health /
        rollback debt を **append 直後**に判定し、`ok=true` の positive health だけを後続へ通す。non-positive は
        `startup_health_unconfirmed` として post-append inventory read-back / pair attestation / readiness / dispatch の前で
        zero-send にし、同 action の public rollback command を pointer として示すだけで auto rollback / auto close しない。
        `None` は startup result を持たない legacy non-Herdr adapter の互換値に限り、Herdr 成功の代用にしない。
        **nested caller** (`sublane prepare-bound-pair --execute` の #13933 v1 replacement-binding 収束、#13948 R3) も同契約に従う:
        fresh replacement participant が bounded startup health に達しないと v1 adapter は `replacement_binding_launch_unhealthy` で
        fail-closed し、その **inner `SessionStartResult`** を catch site (`heal_lane_column`) で locator-free な startup observation
        (同一 startup `action_id` / role health / rollback debt) へ projection して outer の public `prepare-bound-pair` outcome まで
        **losslessly** 伝播する。outcome は同 action の pointer `mozyo-bridge herdr session-rollback --action-id <id>` (`--execute` なし)
        を示すだけで auto rollback / auto close しない。raw locator / launch detail / secret は outer へ出さない。public rollback 完了後、
        同 replacement action binding は rolled-back reservation を正に認識し、次の explicit replay で fresh pair launch へ収束する
        (rolled-back でない replay は startup debt で fail-closed のまま)。rollback debt は **fresh_launched participant** に限り、
        adopted / surfaced sibling は zero-close (pointer も出さない)。
        adopted / foreign / newer / identity drift / duplicate / obligation-present / busy / unreadable は zero-close。
        composer は 3 値を保つ: 捨てられるのは **transaction 自身が生んだ startup UI** (認識済み provider startup blocker で、
        誰も打鍵していないもの) だけで、LLM / operator が投入した composer body は **owner approval の有無に関わらず preserve**
        する。generic pending-composer discard authority (#13918 / #13933 の `direct_owner`) は**拡張しない**。
        trust screen には**回答しない** (承諾は provider 自身の UI 上の operator action)。
      - close の return code は不在の証明ではない: close 後に fresh whole-unit remeasure で participant の positive absence を
        確認し、durable completion write が成功したときだけ rollback success。close 失敗 / residue / 不読 / completion write
        失敗は role 別 non-success として残し、debt を保持する (中断した rollback は再実行で resume する)。
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

### declared pins の role 語彙

`ProcessGenerationPin.role` の **canonical vocabulary の正本は `src/mozyo_bridge/core/state/lane_pin_role.py`**（Redmine #13920）。
pin model は `role` の非空と `(role, provider, assigned_name)` の重複だけを検査し、**語彙は一切検査しない**。よって
writer と reader が綴りを違えても例外にならず、reader が「pins 無し」と読むだけになる（#13920 の defect: adopt 済み
lane が hibernate すると pins が row にあるのに `hibernated_record_missing_pins`）。語彙は各 call site が import 元から
推測する事実にしない。

```yaml
declared_pin_role:
  owner: src/mozyo_bridge/core/state/lane_pin_role.py
  canonical: [PIN_ROLE_GATEWAY='gateway', PIN_ROLE_WORKER='worker']   # 新規 write は必ずこれ
  legacy_read_compat: {codex: gateway, claude: worker}                 # 読むだけ。write では使わない
  意味: role は **slot 名**であって provider ではない。writer は binding に関わらず gateway slot に
        gateway を打つので、legacy 綴りの `codex` も「gateway slot」を意味する (provider は pin の
        `provider` field が持つ)。よって legacy → canonical の read-compat は健全。
  非owner (同名・別値。pin には使わない):
    - domain/sublane_lifecycle: GATEWAY_ROLE='codex' / WORKER_ROLE='claude'
      # 値は herdr assigned-name の role segment (= provider token)。pane routing / name decode /
      # provider 値が依存するため **repoint 不可**。pin 用途で import しない。
    - domain/pair_launch_attestation: GATEWAY_ROLE / WORKER_ROLE
      # launch-attestation slot label。値は owner と一致させて維持する (domain leaf の import 非依存設計)。
```

**read-compat / write-canonical**（#13844 / #13882 と同じ discipline）: read は legacy を受理して広く、write は
canonical のみ、**read が row を書き換えることはない**。明示的な再宣言 rail は `sublane repair-pins`（#13879）。

**「非空」は証明ではない**。`resolve_declared_pin_pair` は pair を一意に名指さない形をすべて fail-closed reason で
返し、consumer は zero-close / zero-send で停止する。

```yaml
pin_pair_fail_closed:
  declared_pins_absent:      pins が無い (create 時 issue lane / v4→v5 の pins-only gap)
  declared_pins_unreadable:  snapshot が decode 不能 (corrupt / newer envelope)
  foreign_pin_role:          どちらの語彙にも属さない role
  mixed_pin_role_vocabulary: 1 row に canonical と legacy が混在 = 別々の writer が触った → provenance 不明
  duplicate_pin_role:        2 pin が同一 canonical slot へ解決 (例 codex + gateway)
                             # pin model の stable_identity dedupe は raw role で見るため検出できない
  incomplete_pin_pair:       片側だけ
```

`recover-pair` の blocked reason は `hibernated_record_missing_pins:<上記 reason>` を報告する（ambiguous な非空 row と
真に pin の無い row は同じ fail-closed でも別の operator 問題であるため）。

**consumer 境界 — slot label と herdr identity role を混同しない**（#13920 review j#80598 F1）。同じ「role」という語が
2 つの異なるものを指すため、consumer は自分がどちらを要求しているか明示する。

| 概念 | 値 | 出所 | 使う場所 |
| --- | --- | --- | --- |
| **slot label** | `gateway` / `worker` | `ProcessGenerationPin.role` (owner: `lane_pin_role`) | pin の role 引き、`stable_identity` 照合 |
| **herdr identity role** | provider token (`codex` / `claude`) | assigned-name の role segment / startup self-attestation の `role` | `evaluate_attestation(expected_role=...)`、live 行の provider 解決 |

- `operator_startup_gate_producer` は pin を **provider で選び**、`runtime_role` に pin の **slot label** を格納する。
- `operator_startup_resume_target` は `runtime_role` を `stable_identity` 照合に使い、**self-attestation の
  `expected_role` には `provider_id` を渡す**（attestation が記録するのは herdr identity role = provider のため）。
  ここに `runtime_role` を渡すと canonical pin は必ず `ATTEST_CONFLICT` になり、健全な lane が resume 不能になる。
- live 行は slot label を持たない。declared pin の `role` は **宣言の属性であって live の観測値ではない**。

### Worker dispatch action-time admission (#13846)

`sublane dispatch-worker` は assigned name / locator / `stale_named_slot` の単独観測を送信権限にしない。送信直前に
current lifecycle generation・current Redmine decision anchor・startup self-attestation の identity/locator generation・
current lifecycle の exact declared worker process pin・receiver runtime state・replacement action binding・同一
issue/journal/receiver の既送達/不確実 delivery を一つの
observation に join し、次の typed decision を返す。

- `healthy`: 全 authority が一致し receiver が dispatch-admissible。送信直前の再観測も byte-equivalent な場合だけ 1 回 inject。
- `stale_worker_recovery_required`: current lifecycle / decision anchor / declared worker generation / action authority が
  すべて current で、その generation の exact slot の terminal absence が positive に証明された場合だけ。
  close/relaunch はせず、owner-governed #13806 recovery へ route する。★**post-close resume 入口**（#13806
  close-success → launch-failure → replay 訂正, IR j#81810 / R3-F1）: recover-stale の `--execute` は、既に close を commit した
  worker（participant が `close_owed` を越え `launch_owed` / `verify_owed` / `replaced`）を持つ durable transaction が
  **その exact approved recovery（同 workspace+action_id+generation）に存在する場合だけ**、pinned old locator の
  absence（fresh-recovery preflight の `identity_unknown`）を **expected post-close state** として durable owed resume
  （launch → attest → 元 gate exactly-once redispatch）へ接続する。admission は **`identity_unknown`（expected old-locator
  absence）のみ**に閉じる: worktree unreadable/dirty・stale generation・productive・gateway/foreign 等の他 blocker は
  old slot が resolve した real な current-state fence ゆえ resume で迂回せず block を維持する（R3-F1）。resume authority は
  admission で snapshot せず、**各 owed effect の直前に action-time 再 join する**（Review j#82731 F1、`runtime-observability-boundary.md`
  Action-Time Live Preflight Boundary）: (1) **launch 直前**は actuator の injected `launch_authority` が `resume_lane_authority`
  （exact lane lifecycle rev/gen + canonical worktree token + expected branch）AND `lane_free_of_live_process`（assigned-name の
  SLOT_LIVE 行=busy OR idle foreign を排除）を再 join、(2) **send 直前**は `_redispatch` が **attempted CAS + lease reauth の後・
  transport 直前（最後の external observation）** に `resume_lane_authority` を再 join する（Review j#82760 F1: attempted の前だと
  CAS/lease-read の間の last-mile authority race を lease check が捕捉できない）。send 直前 check が moved なら send は起きていない
  ので `release_drain_attempt`（`draining_continuation → replacing_nonself` の唯一の guarded 逆遷移、linear DAG は不変）で attempted を
  **un-record** し、re-run が exactly-once 送達できる（`draining_continuation`=send-in-flight 誤認で永久 uncertain にしない）。★**release CAS の outcome は無視しない**（Progress blocker j#82768）: concurrent write で `expected_revision` が stale 化し revert が拒否され得るので、`_redispatch` は row を **再読して state 別 typed disposition** へ分岐する — exclusive lease 保持中は fresh revision で bounded retry すれば re-sendable（`replacing_nonself`）へ収束、収束不能な state は具体 typed blocker（`lease_lost`/`generation_mismatch`/`not_found`、concurrent 完了は `confirmed`）に落とし、**cap 超過 / unexpected phase / unexpected refusal reason は zero-send CAS-recovery failure 固有の distinct token `release_refused`**（Review j#82782 F1）に分離する（send-in-flight `uncertain` とは型で区別、send は当該 invocation で proven-zero、false `authority_moved` を作らない）。silent な `draining_continuation` 残置を作らない。moved/newer lifecycle・wrong worktree
  token・drifted branch・unreadable worktree・foreign live（busy/idle）は zero launch/send で durable transaction を温存
  （lease は actuator が effect 直前に別途再認証、launch 後の自 fresh worker は action-bound attestation が正当性を証明）。

  ★**continuation transport reservation**（#14741 R10）: transaction と lane authority を有限回交互に読むだけでは、最後の
  read 中にもう一方が動く窓を消せない。continuation send は `state.sqlite` native component
  `replacement_continuation_outbox` に、exact `(workspace, action_id, action_generation, source, issue, journal,
  expected_gate, next_action)` を送信前予約する。`replacement_transactions` の exact generation / pointer /
  `draining_continuation` / holder / live lease 検証と UNIQUE reserve は同一 `BEGIN IMMEDIATE`。予約は effect より先に
  commit し、crash 後も `reserved` を自動再取得しない。★**global `state.sqlite` write lock を external transport 中に保持しない**
  （#14741 R11）: exact `(workspace_id, action_id)` の OS advisory fence を全 `ReplacementTransactionStore` mutation と
  continuation consume が共有し、same-action completion / lease / generation mutation だけを actual send 後まで直列化する。
  fence の lock authority はDB inodeではなく、resolved parent + state filename の **stable canonical pathname** + exact action digest。
  したがって `state.sqlite` のatomic replacementでも同じlockを使い続ける。state-file symlink / hardlinkはfail-closed、
  parent-directory symlink aliasはcanonical pathnameへ収束し、same-thread alias re-entryもblocking前にfail-closedとなる。
  lock fileはcanonical DBの隣接owner-only private directoryに置き、秘密値を名前へ含めず、runtime中はunlinkしない。
  final state pathはlock座標の算出時だけでなく、public/internal schema ensureのwrite-capable open前、lock取得後、
  schema ensure前後、実SQLite connectionのopen後にも
  canonical regular file / single linkであることを再検証する。したがって座標確定後のregular→symlink / hardlink差替えは
  transaction mutationまたはcontinuation sendへ到達する前にfail-closedとなる。
  ★`replacement_transaction` component v2 はrow shapeをv2専用authority tableへ移し、旧table名をread-only互換viewとして残す。
  migration前にadmission済みのv1 writerも旧名へのUPDATE/INSERT/DELETEをDBが常時拒否し、v2 writerだけが新実表を使う。
  さらにOS fenceに加えてexact-action effect tableとv2実表のINSERT/UPDATE/DELETE guard triggerを所有する
  **behavioral write protocol**。continuation consumeはtransport前の短いtransactionでDB fenceをarmし、
  outcome確定またはproven-zero時の短いtransactionでexact token/generationを条件にdisarmする。これによりv1 admissionを
  migration前に通過して停止していた旧writerも、send中に再開したmutationをDB自身が拒否する。v1→v2は通常のread/write
  ensureから暗黙実行せず、旧processを停止したoffline rollout railだけがbackup-first明示migrationを呼ぶ。backupはmain DBの
  file copyではなくSQLite backup APIでWAL committed stateを含むstaged logical snapshotを取得し、schema・全authority content・
  storage classをsourceとreadback照合した後にdirectory renameでatomic publishする。fresh v1-only runtimeはv2 stampで
  fail-closedする。
  SQLite の `BEGIN IMMEDIATE` は transport 前後の短い validation / outcome write に限定するため、遅い1送信が別action・別laneの
  state writeを2秒timeoutさせない。action fence は transport railから同action transaction writeへ再入不可（fail-closed）で、
  現行4 production railはlane authorityをread-only、delivery/attestationを別DBへ書く。short transactionでowner /
  transactionを確定後、最後のlane authority観測→fresh clockによるcached live lease判定→actual send invocationの間には
  DB connect / schema ensure / blocking observationを置かない。authority観測中にleaseが失効した場合はsend 0・stale holderによる
  release/revert 0。typed `zero_send` のrevert判定はSQLite writer lock取得**後**のfresh clockで行い、DB待機中にleaseが失効した
  holderを古いtimestampで再認証しない。
  `authority_moved` / rail-proven `zero_send` のみ、**その時点でもliveな** owner token/holderを条件にoutbox row の削除と
  `draining_continuation → replacing_nonself` を同一短時間transactionで行う。send が開始した可能性があれば
  `delivered` / `uncertain` は非再取得 terminal であり、自動再送しない。exact-generation `completed` は lossy ledger read より
  強い単調 terminal として全 shared caller が `confirmed` へ投影する。

  worktree readability は exact worktree-token authority の一部（単なる git-checkout 解決だけでは sibling/wrong worktree を通す）。
  ★**action-time 再 join は「close 前 preflight を持たない理由」にならない** (#14475、live blocker #14462 j#88463)。launch authority が
  action-time にしか無いと、**launch より前に destructive close がある** guarded refresh では「preflight 全 green → close commit →
  `launch_authority_moved`」で relaunch 不能な状態に落ちる。したがって同 authority は **close 前の read-only preflight 軸**
  でもあり、両者は `lane_authority_reason`（closed typed vocabulary）という **単一 evaluator** を共有する — preflight を別実装で
  近似すると、preflight が予測しているはずの effect から drift する。action-time 再 join は撤去せず維持する（pre-close read は
  close と launch の間に起きる **遅い** authority move を予測できないため）。preflight 軸が加わっても post-close replay の
  admission（`identity_unknown` + committed-close transaction のみ）と exactly-once resume 契約は不変。
  **dirty（but readable）worktree は byte 保存対象ゆえ authority 軸でない=block しない**（Design Consultation Answer j#82708
  Option A / tranche D contract, IR j#79485 §4 / `assess_worker_recovery_preservation`; 初回 close_owed と retry launch_owed で
  同一 policy、launch failure timing で挙動を変えない）。durable transaction 不在 / `close_owed` 止まり / generation 相違は
  resume と認めず block を維持する（新規 plan も blind launch もしない）。owner re-approval journal は
  stored decision/continuation anchor（同一 CAS identity を保つ `--journal`）と **別 pointer**（`--resume-journal`）で
  表し、same-action CAS と fresh durable approval を両立させる。
- `worker_liveness_authority_conflict`: locator-bearing stale token、duplicate row、missing/ambiguous declared pin、declared
  locator/provider/name/runtime-revision drift、missing/foreign attestation、generation/action drift、busy/unknown receiver、
  既送達または送達不確実を含む。それらはすべて zero-send / zero-close / no auto retry。replacement action id が空の
  normal launch は、exact declared process pin が一致する場合だけ lane generation binding を authority として使う。

queue-enter / transport ACK と worker uptake は別の causal fact である。`worker_dispatched=true` は admission=`healthy`、
transport=`sent/ok`、かつ exact delivery に結び付く event-driven turn-start=`started` の連言だけで記録する。ACK 後に
turn-start が観測不能なら `turn_start_unconfirmed` とし、inject 済みの可能性があるため自動再送しない。

#### Recovered pair の lifecycle decision と work anchor (#14203 R18)

通常の `dispatch-worker` は current lane decision と dispatch anchor が一致する same-lane gateway
flow のまま維持する。一方、hibernated lane を recovery approval で `active` へ戻した generation では、
`lane_lifecycle.decision_journal` は **resume を決めた durable record** を指し、worker に渡す
`implementation_request` journal とは異なる。ここで lifecycle pointer を work anchor へ上書きしては
ならない。上の `lane_lifecycle_records` 契約どおり、pointer は「現在の state をどの durable record が
決めたか」の authority だからである。

この限定形は `sublane recover-worker-delivery` が別責務として扱う。public preflight / execute は
active lifecycle issue・generation・decision journal、declared gateway+worker pair、両slotの
live/settled/startup-attestation/replacement-action binding、retirement、unchanged work anchorを別axisで
joinする。さらに、standard forward が typed/send/turn-start/target 0だった strict evidence journal と、
その exact lifecycle decision / work anchor / worker generationをdigestした action-specific owner
authorizationが必要である。new outbox actionは `started` のみ delivered とし、zero-sendはcancelled、
post-injection/unknownはuncertainとして自動retryしない。ordinary sender admission / standard
`dispatch-worker` / lifecycle schemaは緩めない。

#### Rehydrate 時の declared pair snapshot と既存 active residue (#14203 R19)

`hibernated -> active` は fresh pair の live/settled/startup self-attestation を確認するだけでは不十分で
ある。`declared_slots` は「宣言時点の process generation snapshot」なので、resume が採用した fresh
gateway/worker の canonical `ProcessGenerationPin` を **disposition flip と同一 lifecycle CAS** で保存する。
別 transaction にすると `active + old locator snapshot` が観測可能になり、strict generation admission が
健全な fresh pair を stale として拒否する。provider binding、raw assigned-name multiplicity、live surfaced
provider、locator、startup attestation のいずれかが解決不能なら、resume は disposition も pins も
zero-write とする。

この原子的更新より前に作られた `active + old declared pair + action-bound fresh pair` residue は、
generic backfill / `repair-pins` / standard dispatch の緩和では直さない。public
`sublane reconcile-recovered-pair-pins` は、exact issue/lane/worktree、active revision、lane generation、
lifecycle decision、old pair、`recover-pair` source revisionから再構成した action id、fresh pair の
live/settled/attested/action-bound generation、および exact structured owner authorizationを連言した場合
だけ、`declared_slots + revision + updated_at` を bounded CAS で更新する。side-binding v1 では各 old
locator も exact 照合する。disposition、lane generation、decision pointer、issue/worktree binding、
release/replacement axes、process、worktree、branch、message delivery は変更しない。byte-equal new snapshot
の replay だけを idempotent success とし、expected-old / expected-new のどちらでもない snapshot、
revision/generation/decision/action drift は zero-write である。

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
  shared store stays at its current version and every concurrent older reader keeps working. The whole read — component status /
  recorded version / table+index signature / compatible `SELECT` construction / the caller's row `SELECT` — runs inside **one
  explicit read transaction**, begun before the first schema query and held until the connection closes, so every statement
  observes the SAME committed store state (Redmine #13844 R9 j#79848: in autocommit each statement takes its own snapshot, and a
  peer migration committing between the version read and the signature read yields a torn v5-metadata/v6-shape view that
  misclassifies a healthy authority as partial/corrupt). `busy_timeout` remains only a lock-wait aid for a read landing during a
  peer's migration commit — it is NOT the snapshot authority and does not make separate statements consistent.
- **Every mutation migrates through ONE explicit write gate, preflight BEFORE the migration.** No CAS opens the store with a bare
  `ensure`. Every schema-needing mutation — declaration / incarnation AND disposition / supersede / release / replacement / retire
  / reconcile — opens via the single choke point `LaneLifecycleStore._connect_write(writer_key)`, in strict order: (1) read the
  compatibility **preflight** on the STILL-OLD store (the active peer lanes a forward migration would fail-close, the writer's own
  lane excluded); (2) **BEFORE migrating**, emit the operator advisory (`emit_lifecycle_migration_advisory`) when a migration is
  pending with peers at risk — a genuine PRE-migration warning while the store is still the old version, *not* a post-hoc notice
  (at the moment it fires the recorded version is still the old one and no backup has been taken); (3) run the backup-first
  migration; (4) capture BOTH the pre-migration preflight and the post `created` / `intact` / `migrated{from_version, backup_dir}`
  outcome — kept distinct — on `last_write_preparation`. So a migration is a chosen, visible act with legible peer risk for *any*
  mutation, announced before it happens, never an implicit side effect — and a read that precedes the write never migrates ahead
  of the preflight.
- **The migration is auditable in each command's structured outcome, not only stderr.** The composing-store commands
  (`sublane quarantine` / hibernated-legacy-retire / hibernated-live-reconcile / hibernated-bound-retire) expose the wrapped store's `last_write_preparation`
  and carry `lifecycle_migration_payload(...)` (from/to version, backup, peer-reader risk) in their JSON/text outcome, so a forward
  migration is legible in the command's audit record for replacement / retire / reconcile alike — matching the universal
  pre-migration stderr advisory above. `last_write_preparation` is **most-recent** (this write's), NOT a store-lifetime
  accumulator: a store instance may be reused across operations, so a lifetime accumulator would let a later read-only / intact
  action inherit a PAST migration and fabricate a side effect it did not perform. Preserving the migration across a *single*
  command's several writes is the **use case's** job, **operation-scoped** — reset at the start of each run and folded in after
  **each** of that run's schema-needing writes (a command can migrate on any of them, e.g. a quarantine *redrive* of an existing
  generation migrates on its first `record_replacement_outcome`, not on `request_replacement`) — so a preflight-only run, or a
  reused store whose earlier run migrated, reports nothing. A fail-closed
  (CAS-refused) verdict that nonetheless migrated the store scopes its "zero-write" claim to the lane ROW and reports the schema
  migration as a separate side effect — an audit record must never deny a side effect that happened, nor invent one that did not.
- **Fail-closed is preserved and made specific.** An unknown / newer / partial / malformed shape still fails closed (no
  downgrade, no misread), judged only by the **shape / capability table** (`_ALLOWED_SHAPES_BY_VERSION` / `_COLUMN_DEFS`), never a
  guessed compatibility. The specific NEWER sub-case is named `reader_upgrade_required`: the store is fine, THIS reader is stale,
  so the caller routes to the **current compatible high-level facade** (the up-to-date source CLI) rather than a raw DB downgrade.
- **Source CLI vs installed facade boundary.** An unintegrated issue-branch source CLI is a *reader* of the shared authority; it
  must not implicitly migrate it. A schema-changing write (a mutating command) should surface the **compatibility preflight**
  (`lifecycle_migration_preflight`) — read-only, version-compatible — which reports the other active lanes a forward migration
  would fail-close, so the migration is a chosen, visible act rather than an accidental side effect of a background notification.

#### attestation store: read-compatible / write-conservative (mixed-runtime shared home — #13882)

`herdr-identity-attestation.sqlite` takes the read half of the #13844 contract above and **deliberately inverts the write half**.
The reason is a boundary no other component has: it is the one home store written by a process this runtime did not build. A
managed launch wraps the provider through `<attest_launcher> herdr agent-attest …` and injects
`--env MOZYO_BRIDGE_HOME=<store_home>` (`herdr_launch_argv`), so **whichever `mozyo-bridge` the operator happens to have installed
writes into the same file this runtime reads**. Its readers are not all shipped in this build, and cannot be migrated in step with it.

Live evidence (#13882): the shared home held the pre-0.12 **v1** shape while the runtime required v2. The old exact-version write
guard raised, the best-effort writer swallowed it (an agent boot must never be blocked by a store failure), and a fresh pair booted
**live but unattested** — `partial_pair_recovery_required`, with re-adopt (`unattested_slot`) and `recover-pair` (`missing_pins`)
both refusing. 94 genuine v1 rows read as `absent`. The #13847 capability preflight could not see any of it: it joins the launcher's
*advertised* schema against the *source runtime's required* schema — both **code** — and never opens the store on disk.

- **Reads never migrate, and project older shapes up.** Identical to #13844: `readonly_compatible_select` emits the current column
  vocabulary, padding a column an older shape lacks with its migration-default literal, inside **one explicit read transaction**
  begun before the first schema query (the R9 torn-snapshot discipline). A v1 row's empty `replacement_action_id` is not a guess:
  its writer predates the replacement transaction (#13806) entirely, so `''` is that row's **true** value. Padding it is a proven
  backward-compatible projection; the field-drop hazard the pre-#13882 comment feared lives on the *write* side, and is refused there.
- **Writes never migrate either — the deliberate divergence.** In `lane_lifecycle`, every mutation migrates through one write gate,
  which is safe because all its readers ship in this build. Here, forward-migrating the shared home on a launch would leave every
  **older installed launcher** hitting its own exact-version guard, silently dropping its attestation and booting live-but-unattested:
  the identical defect, merely inverted onto the old runtimes. So a recognized older store is written in **its own shape**
  (`writable_projection`) and left at its own version. Forward migration is an **explicit operator command only**, never a launch
  side effect.
- **v3 adds `lane_epoch` (#14756) under the same contract.** A v1/v2 row truthfully carries an EMPTY epoch (its
writer was never handed one), so the read projection pads `''` — never a number, which would be a threshold claim the row
never made. The write side gains the second instance of the refusal below: an **epoch-bearing** launch onto a v1/v2 store
raises (`write_drops_lane_epoch`) rather than landing a row with the epoch dropped, because such a pair would be live,
correctly launched, and permanently unresumable. An **epoch-less** launch still writes the older shape, so the
mixed-runtime home keeps working exactly as #13882 requires. The operator rail is the same explicit
`mozyo-bridge herdr attestation-store migrate --write`.
- **The one write refusal is the field that cannot be dropped.** A **normal** launch (empty `replacement_action_id`) writes the v1
  shape losing nothing. A **replacement** launch (non-empty `action_id`) is refused there and raises
  (`write_drops_replacement_action_id`), because a dropped binding would leave a fresh worker a replacement recovery matches on
  exactly (`sublane_stale_worker_recovery_live`) permanently unverifiable. The best-effort writer still never raises into a boot —
  the refusal is surfaced by the **preflight, before the launch**, not by crashing the child.
- **The bounded-pair v1 recovery exception does not drop that field** (#13933 R12). General replacement launches remain refused on
  v1. The reviewed hibernated bound-pair convergence rail instead reserves a separate home-scoped action-binding row **before** its
  launch, writes the fresh process's ordinary v1 self-attestation, then binds only after the startup transaction proves one exact
  successful participant receipt. The immutable join is action + assigned name + workspace + role + lane + old locator + startup
  nonce/action, and the bound join additionally pins the fresh locator and attestation timestamp. A retry resumes that same durable
  startup receipt; an already-live slot with no prior reservation is foreign and is never adopted or retroactively bound.
  Publication is an atomic private `0600` SQLite v1 side store with strict shape validation and compare-and-set writes; it never
  migrates or repairs an unknown shape. The main-store shared generation lock spans v1 selection, reserve, launch/self-attestation,
  receipt read, and bind, so explicit maintenance cannot overtake the action. Readers accept the side binding only while the selected
  main store is still recognized v1 and every identity/generation field matches. Native v2 continues to carry
  `replacement_action_id` directly; migration, a different action/generation, unreadable authority, unsafe permissions, or a torn
  schema all fail closed. This is therefore a two-record exact binding, not an implicit migration or a silent field drop.
- **Admission joins the launcher against the REAL store, before any actuation.** `probe_store_schema` (read-only; creates nothing;
  an unopenable file is `store_unreadable`, never folded into "absent") + `decide_store_compatibility` run at the #13748/#13847
  preflight boundary in `prepare_session`, i.e. before the first herdr `workspace` / `tab` / `agent` write, so an incompatible store
  aborts with zero herdr side effect. Admitted: absent store; exact-version store; older store + normal launch + a launcher that can
  prove it writes that shape. Refused: unreadable; unsupported (naming *upgrade* vs *corrupt* honestly from `reader_upgrade_required`);
  replacement launch onto a pre-`replacement_action_id` shape; a launcher that cannot prove it writes the store's shape.
- **The launcher advertises the writable SET, not just its native schema.** The #13847 token (`mozyo_attest_capability_schema=`)
  carries one exact version, which **cannot distinguish** a pre-#13882 build (writes v2 only) from a #13882 build (writes v1
  conservatively) — both advertise `2`, yet only the second is safe against a v1 home. So `agent-attest --help` additionally
  advertises `mozyo_attest_capability_stores=1_2` (underscore-separated: argparse help wrapping breaks on hyphens and width). A
  launcher advertising no set is credited with its **native schema only** — fail-closed, because that is precisely the build whose
  write guard is an exact match.
- **Migration / rebuild is public, backup-first, and consumer-gated.** `mozyo-bridge herdr attestation-store {status,migrate,rebuild}`
  is the only supported rail (raw SQLite editing is a #13882 non-goal). Read-only plan by default; `--write` acts. `migrate` is
  additive and idempotent; `rebuild` rotates an **unreadable / unsupported** store into `backups/` and is refused for a recognized
  older store, which `migrate` handles without discarding rows. Rebuild is legitimate only because this component's recovery policy
  is `rebuildable_cache`: the next launch's self-attestation re-derives it, and until then reads degrade to fail-closed (adopt
  refuses, doctor non-green), never to a false attestation. Both mutating intents refuse while a **proven consumer** is live, and the
  **live-zero read runs before** the idempotent already-current success, so a replay never reports success while consumers are live
  (#13841 j#79150 finding 2). An **unreadable** inventory is not an empty one and refuses just as hard. Neither intent closes, sends
  to, or launches a process.
- **"Active consumer" is scoped by evidence, not by guess — and is tri-state.** herdr exposes no surface returning a launched
  process's environment (the constraint this whole component exists to work around), so *which home a live agent was launched against
  is unobservable*. A **stored row is the only proof** that ties a live agent to this store, so a consumer is an agent that is live
  **AND** carries a record here — cross-workspace, never repo-scoped, since the store is shared. Counting every managed agent on the
  server instead is not "more conservative", it is wrong in both directions: it refuses an unrelated home forever (measured: 18 live
  agents blocking a scratch home none of them had ever written) without protecting anything extra. Excluding a live agent with no row
  here is not a fail-open, because attestation is a **one-shot write at boot** (`perform_self_attestation` is the sole production
  writer and `exec`s immediately after): a live agent has already completed its only write, so it either uses another home or already
  failed to attest, and in neither case can this store's shape degrade it further.
  The measurement is `no_consumers` / `consumers` / **`unmeasurable`**, and the precedence matters (#13882 review j#80000 finding 2).
  An **empty fleet is proof of none** whatever the store's state — nothing can consume a store when nothing is running — so it is
  checked *before* the store is read, which is what keeps `rebuild` reachable at all. Only when agents ARE live does readability
  matter: an unreadable store's rows cannot be enumerated, so the intersection is unknown and the honest answer is `unmeasurable`,
  never "none". Folding it to an empty set fails open on precisely the destructive `rebuild` path, whose entire target set *is*
  unreadable stores — the same "unreadable is not empty" rule already applied to the inventory, which the first implementation
  applied there but not to the store.
- **The migration snapshot is a SQLite backup-API copy, not a file copy** (#13882 review j#80000 finding 1;
  extended to `lane_lifecycle_backup.backup_state_container` by #14756 j#96956 F5, which additionally stages the
  snapshot, verifies it against a schema+content digest that carries each cell's storage class, and publishes by
  atomic rename — a count-only readback published a snapshot whose cells differed, #14756 j#96973 R11-F6). `shutil.copy2`
  duplicates only the main DB file, so a store in WAL mode leaves committed pages in `-wal` and the snapshot loses them —
  reproduced: a v1 store with one committed row under `journal_mode=WAL` / `wal_autocheckpoint=0` produced a recovery point reading
  `version=1, rows=0` while the live store held the row. A recovery point that is incomplete *and trusted* is worse than none.
  `Connection.backup()` is transaction-consistent and checkpoint-independent, so the snapshot is judged by **content** (version +
  rows), not bytes. (`state_store._backup` still uses the file-copy
  shape; that is pre-existing and out of this component's scope, but shares the hazard.)
- **The two preservation rails are split by CALLER INTENT, never by exception type** (#13882 review j#80029 R2-F1). The logical
  snapshot (`backup_attestation_store`, used by `migrate`) has **no fallback**: any failure raises and the migration aborts with the
  store untouched. The raw byte quarantine (`quarantine_attestation_store_artifacts`, used only by `rebuild`) runs only where
  `probe_store_schema` has *already proven* the store unreadable — a file with no logical snapshot to take, whose bytes are the
  evidence an operator needs. The first fix instead inferred "not a database" from `sqlite3.DatabaseError` and fell back to a byte
  copy; but SQLite raises that same type when a **valid** database is busy or its I/O fails, so the type cannot distinguish the two.
  Fault-injecting a lock error into a valid WAL store's `backup()` made the migration report `migration_applied` while writing a
  `rows=0` recovery point — the original F1 defect regenerated through its own fix. **Corruption is a caller's conclusion from a
  probe, not an inference from an exception.**
- **Preservation and rotation are whole-artifact** (R2-F1(b)). A crashed WAL writer leaves `-wal` / `-shm` beside a corrupt main DB.
  Copying only the main file stranded that evidence in place while `rebuild` removed its sibling; leaving a `-wal` behind after
  removing the main DB would also let a later open resurrect a partial store from the orphaned sidecar. Both the quarantine and the
  removal cover the main file plus every existing sidecar.
- **A backup is STAGED and published atomically; a failure publishes nothing** (#13882 review j#80045 R3-F1). `backups/<ts>/` is the
  namespace an operator trusts as a recovery point, so building the artifacts directly inside it means every failure publishes a
  partial one — measured: a snapshot raising mid-`backup()` left an 8192-byte DB with `user_version=0` sitting in `backups/`, and a
  quarantine whose sidecar copy failed left a "whole-artifact" recovery point holding only the main file. Both rails therefore build
  under `.backups-staging/` — a **sibling** of `backups/`, never a child, so even a failed cleanup leaves nothing discoverable in the
  published namespace — and publish with a single `os.replace` of the directory once every artifact is complete. A reader of
  `backups/` sees the whole set or nothing. The rule generalizes the section's own principle: *incomplete and trusted is worse than
  none* applies to the failure path too, not only the success path.
- **The staging directory is reserved atomically and owned exclusively; the destination is allocated at publish time** (#13882
  review j#80081 R4-F1). "Whole set or nothing" must hold under **concurrency**, not only against sequential failure. Deriving the
  staging name from the second-resolution stamp and `rmtree`-ing that guessed path — intending to clear a prior crash's leftovers —
  let two quarantines starting in the same second share one staging dir: the later deleted the earlier's *active* tree, and the
  earlier then published the later's partial bytes as a complete recovery point while returning success (measured: the published
  artifact held 7 bytes of a peer's half-written copy). A guessed path can belong to a live peer, so the answer is not a better
  guess but **never guessing** — `mkdtemp` reserves a unique directory atomically, and no operation ever removes a tree it did not
  create. Symmetrically, reserving the final name up-front via `exists()` was a TOCTOU that *guaranteed* collision rather than
  avoiding it (the final dir does not exist until publication, so concurrent operations always chose the same name); each candidate
  is instead *attempted* at publish time, and because a published recovery point is always non-empty, `rename` onto a peer's
  directory fails `ENOTEMPTY` and yields the next suffix instead of clobbering it.
- **The preserved artifact set is pinned once, before the first copy** (#13882 review j#80103 R5-F1). Owning the *destination* is not
  enough if the *source* is re-observed as you go: deciding each sidecar's fate with an `exists()` evaluated just before copying it
  put a TOCTOU between **choosing** what to preserve and **preserving** it. A peer rebuild rotating the store in that window made an
  operation skip sidecars that were present when it started and publish a **main-only** directory as a complete recovery point with
  `state=applied` (measured). The manifest is therefore fixed up front and becomes this operation's promise; any artifact from it
  that has since vanished is a hard failure — the quarantine fails closed and publishes nothing. So a published recovery point holds
  everything observed at its own start, or does not exist, and a racing peer fails closed rather than publishing a remainder.
- **Three boundaries share one advisory lock; that exclusion — not any per-operation care — is what pins a generation** (#13882 review
  j#80159 R7-F1, design answer j#80190, supersession j#80207). Every guard above still addressed the store **by path**, and no amount
  of care inside one operation can pin a *generation*: the probe approved one store, a peer rotated it, a fresh one appeared at the
  same path, and the stale run backed up and deleted that fresh, valid store, reporting `applied` (measured, both windows —
  probe→quarantine and backup→remove). POSIX has no identity-conditioned unlink, and a `rename` "claim" is equally path-addressed, so
  the fix is exclusion, at all three boundaries that touch the store:
  - **managed-launch admission** (`prepare_session`) takes it **shared, non-blocking**, before the first attestation read and held
    through the last actuation. Maintenance in progress therefore fails the launch **at acquisition** — no workspace / tab / agent —
    and, because admission holds it for the whole run, maintenance can never overtake an in-flight launch.
  - **self-attestation write** (`upsert`, whole region, before the store is even opened) takes it **shared, blocking**. It waits;
    it does not degrade. Turning contention into a best-effort failure would drop the attestation and recreate this component's own
    defect, so only a *genuine* store/platform failure (no `flock`, unopenable lock file) still degrades to an absent record —
    contention never does. The #13637 contract that a store failure must not stop a boot is unchanged: waiting for a named peer is
    coordination, not failure.
  - **maintenance mutation** (`migrate --write` / `rebuild --write`) takes it **exclusive, non-blocking**, before the initial probe
    and through the result decision. It never queues ahead of a live launch or write: it reports blocked, publishing and removing
    nothing.
  The lock is a home-scoped file of its own (`.herdr-identity-attestation.lock`, `0600`) — never store content, never part of a
  backup manifest, never credential-bearing. A holder's crash releases it at the OS level, so no dead process can wedge the store.
  Where `flock` is unavailable the protocol cannot be honored and the operation **fails closed** rather than proceeding unlocked: a
  silent no-op would advertise a guarantee that is not there. Regressions for this live across **real processes** — a thread-level
  mock proves nothing about a per-process kernel primitive.
- **Residual, bounded and documented** (j#80190 Q2, j#80207): a runtime of another vintage does not know this protocol, so an
  *uncooperative old-runtime launch concurrent with explicit maintenance* is not excluded, and this component must not be described
  as "race-free" without that qualifier. The residual is bounded to exactly that case — normal mixed-runtime reads and writes are
  unaffected, and the current lock-aware rail's races are closed. The operator-facing precondition is simply: do not start
  non-lock-aware launchers while a maintenance command runs. Closing it mechanically would need a generation-directory layout or a
  launch-admission fence, which is a separate scope, not a blocker here.
- **"Nothing to preserve" after a non-absent probe means backup-first is UNPROVEN, not satisfied** (#13882 review j#80129 R6-F1). When
  the probe saw a store but the quarantine finds none, the store vanished in between — a peer's rotation, or something outside this
  rail. Treating that as "no backup needed" and continuing reported `state=applied`, `executed=True` and a detail claiming the store
  was *rotated into backups/* while **no backup directory existed at all** (measured): fabricated recovery evidence, the exact mirror
  of the denial R3-F2 closed — the same rule forbids both. The rebuild fails closed there (`executed=False`, nothing published,
  nothing removed) and says so; a re-run converges, since its probe then reports `STORE_ABSENT` → already current. **`APPLIED` must
  always imply a real recovery point on disk.**
- **Removal is idempotent (`missing_ok`), never `exists()`-guarded** (same family). An already-absent artifact *is* the rotation's
  goal state, so erroring on it denies a side effect in the opposite direction: a peer's unlink landing between the check and ours
  turned a fully-achieved goal state into a false "interrupted, re-run" report.
- **The main file is the rotation's completion sentinel: sidecars first, main LAST** (R3-F2). `probe_store_schema` decides a store
  *exists* by the main file, so removing it first made a half-done rotation indistinguishable from a finished one — measured: a
  failing `-wal` unlink left `main` gone and the sidecar orphaned, the retry then probed `STORE_ABSENT` and reported
  `already_current` ("no store exists"), so the public command declared success while the orphan persisted and no rerun could ever
  clear it. Sidecars-first inverts that: any interruption leaves the main file present, the store still probes as existing, and the
  same command re-run finishes the job — which is what Acceptance 3's idempotency requires. Correspondingly, an interrupted rotation
  **must not report "untouched"**: it reports that artifacts may already be removed and that a re-run completes it (`an audit record
  must never deny a side effect that happened`). "Untouched" survives only on the branch that fails *before* any removal begins.
- **Only a whole, canonical capability token is credited** (#13882 review j#80000 finding 3). Both advertisement tokens are bounded
  on each side and matched against an exact grammar (`<int>` / `<int>(_<int>)*`); a malformed spelling is **not** salvaged into a
  capability. Unbounded matching credited `…schema=2x` as a clean v2, and `stores=1__2` / `_1_2_` / `1_2junk` as `{1, 2}` — handing a
  launcher the v1-write capability that admits a v1 store, re-opening the exact live-but-unattested launch this component refuses.
  **Conflicting** advertisements of the same fact are not arbitrated either: a launcher declaring two different schemas has clearly
  declared neither, so the fact stays unprovable rather than resolving to whichever matched first.
- **Compatibility is judged by the shape table.** As in #13844, `_ALLOWED_SHAPES_BY_VERSION` / `_COLUMN_DEFAULTS` — never a guessed
  version comparison. A recognized version whose on-disk columns disagree is partial / corrupt and fails closed rather than being
  silently repaired.

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

### test / probe は operator 共有 home を解決しない (#14477 j#94504)

**home scope の authority store は複数 lane が同時に読む共有資源であり、test / probe / 診断が
それを forward-migrate してはならない。** schema version を上げた branch の未 review WIP source で
これを起こすと、**他の稼働 lane が全て read-fail-closed する** (`### schema version / migration` の
downgrade-safe 契約により、古い reader は新しい component version を読めない)。実測: #14477 の
v7→v8 bump は full suite 実行中に共有 `state.sqlite` を `2026-07-29T21:46:28Z` へ migrate し、
reviewed v7 facade からの review_result delivery が `reader_upgrade_required` で zero-send になった
(j#94504 / j#94516)。

規則:

- store は **必ず明示的な `home=` / `path=` を渡して**構築する。**無引数構築
  (`LaneLifecycleStore()` 等) は `MOZYO_BRIDGE_HOME` / `~/.mozyo_bridge` を解決する**ため test /
  probe では使わない。write は write-**migrating** gate を通るので、1 回の `declare_active` で
  共有 component が migrate する。
- `patch.dict(os.environ, ..., clear=True)` は `MOZYO_BRIDGE_HOME` を消し、解決先を
  `~/.mozyo_bridge` へ落とす。env を消す test は home を **別途** temp へ固定する。
- migration 自体を検証する test は temp isolated home 上で行う (backup / version stamp / byte
  不変を含めて、そこで完結させる)。
- hermeticity は「意図」ではなく **checked property** にする。store path が temp 配下であること、
  解決される default home store path と一致しないことを test 自身が pin する
  (実装例: `tests/regressions/test_issue_14477_repair_pins_resume_freshness_anchor.py`
  `OperatorHomeHermeticityTest` — 無引数構築の不在は自 module の AST から導出して pin する)。
- 診断で共有 store を読む必要がある場合は **read-only** で開く
  (`sqlite3.connect("file:...?mode=ro", uri=True)`)。downgrade / raw repair / hand edit はしない。
  - **`immutable=1` を付けてはならない** (#14477 review j#94531 R2-F3)。公式 URI 契約
    (https://www.sqlite.org/uri.html) では `immutable` は「file が read-only media 上にあり
    **elevated privilege を持つ別 process によってさえ変更されない**」ことの宣言であり、SQLite は
    **file locking と change detection を全て skip** する。実際に変更される file へ設定すると
    **incorrect query result / `SQLITE_CORRUPT`** を返し得る。home scope の authority store は
    複数 lane / supervisor が並行更新する **mutable** store なので、この宣言は成立しない。
    `mode=ro` は locking / change detection / WAL を維持するので、こちらを使う。
  - **一貫した snapshot が必要な場合**は「読む前に snapshot を作る」。SQLite の
    `VACUUM INTO '<dest>'` または backup API を使う。`state.sqlite` だけを `cp` する方法は
    **WAL / `-shm` を取り残して不整合になり得る**ため snapshot として扱わない。
  - **単発の file 読取 (size / mtime / hash) は transactional snapshot ではない**。並行書込み中は
    torn read になり得るので、「変化していない」ことの根拠に使うときはこの限界を併記する。
    「変更を防いだ」ことの証明は SQLite に依存しない層 (上記 kernel deny の negative proof) で採る。

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
