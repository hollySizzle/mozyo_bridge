# Unit Presentation State DB Boundary

Redmine #11905 / #11909。`UnitRecord` / `TargetRecord` を cockpit や
future projection で扱うときの **desired / presentation state** を、tracked
static file ではなく DB current table として扱うための To-Be schema 境界。

本 doc は schema design の正本である。runtime 実装の first slice は Redmine #12304 で
着手済み (下記 `## 実装状況 (#12304)`)。残りの table / write path は引き続き別 task で
migration、CLI/API、tests を切る。

## 結論

```text
workspace identity      -> registry.sqlite + minimal workspace anchor
runtime liveness        -> live tmux
inventory projection    -> inventory.sqlite cache
desired event history   -> managed-events.sqlite append log
desired current state   -> unit/presentation current tables
workflow completion     -> Redmine journal/status
```

`registry.sqlite` を cockpit / projection state の置き場にしない。
`inventory.sqlite` を liveness 正本にしない。tracked JSON / YAML を unit ごとの
mutable state 保存先にしない。

## 既存モデルとの関係

`vibes/docs/logics/managed-state-model.md` は、mozyo が command boundary で
「何を作ろうとしたか」を append-only event log として記録する設計正本である。

本 doc はその上に載る **current table 境界**を定義する。読み取り面で毎回 event
folding だけに依存すると実装が太るため、operator / cockpit が参照する desired
状態は current table として持つ。ただし event log は audit / replay / rebuild の材料
として残す。

## DB 分担

### registry.sqlite

責務:

- workspace identity
- workspace_id
- canonical_session
- workspace display name
- repo / workspace anchor との対応

禁止:

- cockpit group membership
- projection preference
- pane/window/process/cwd liveness
- pinned / hidden / retired state
- business / private lane policy

registry は identity 正本であり、presentation state の正本ではない。

### managed-events.sqlite

責務:

- managed unit / pane に対する command-boundary intent event
- created / adopted / marked / renamed などの履歴
- current table 再構築の入力

禁止:

- live target existence
- active pane / foreground process の正本化
- Redmine completion state の複製

### presentation state DB

置き場の名前は実装 task で決める。初期案は `${MOZYO_BRIDGE_HOME}` 配下の
SQLite DB とし、workspace registry とは別 DB にする。

責務:

- current desired unit composition
- cockpit group membership
- projection preference
- operator-managed view state
- latest target observation cache

非責務:

- live liveness
- handoff preflight の最終判定
- workspace identity の生成
- workflow completion

## To-Be tables

### unit_desired_state

「この workspace/lane を mozyo がどういう Unit として扱いたいか」の current
state。

```sql
CREATE TABLE unit_desired_state (
    unit_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL DEFAULT 'local',
    workspace_id TEXT NOT NULL,
    lane_id TEXT NOT NULL DEFAULT 'default',
    repo_label TEXT,
    preferred_branch TEXT,
    role_set_json TEXT NOT NULL,        -- e.g. {"codex": true, "claude": true}
    coordinator_role TEXT,              -- e.g. codex
    status TEXT NOT NULL DEFAULT 'active', -- active | retired | hidden
    updated_at TEXT NOT NULL,
    source_event_id INTEGER
);
CREATE UNIQUE INDEX idx_unit_desired_identity
  ON unit_desired_state(host_id, workspace_id, lane_id);
```

正本として扱ってよいもの:

- desired Unit composition
- retired / hidden など operator-managed desired status
- coordinator role preference

正本として扱ってはいけないもの:

- pane_id が今生きているか
- target process / cwd / active split

### cockpit_group_membership

「どの cockpit group にどの Unit を載せたいか」の current state。

```sql
CREATE TABLE cockpit_group_membership (
    group_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    position INTEGER,
    width_weight REAL,
    pinned INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    source_event_id INTEGER,
    PRIMARY KEY (group_id, unit_id)
);
CREATE INDEX idx_cockpit_group_position
  ON cockpit_group_membership(group_id, position);
```

`group_id` は display grouping であり、routing identity ではない。cross-project
cockpit でも Redmine governance / project ownership は各 Unit 側に残る。

Project Group は `unit-target-model.md` の Project Group と同じ projection-only
概念である。`cockpit_group_membership` は Project Group -> Unit の membership /
position / view preference を持つが、次を持たない:

- role-specific delivery target。
- owner approval / review / close / workflow completion。
- Redmine project ownership の二重正本。
- private project path、private host topology、operator 固有 color / layout default。

`group_id` は portable display key として扱う。Redmine project id、repo absolute
path、tmux session name、window nameをそのまま identity authority にしない。必要なら
別 layer が public-safe label / external pointer を join するが、handoff resolver は
group ではなく Unit -> Target preflight を使う。

### desired presentation config

Repo-local config に置いてよいのは、portable で review 可能な desired presentation
preference だけである。

許可:

- known Project Group の default display label / sort key。
- workspace / repo label から group membership を導く public-safe rule。
- pinned / hidden / preferred projection の初期値。

禁止:

- handoff target resolution。
- owner approval / review / close / workflow truth。
- live pane id / session geometry / active split。
- private absolute path、private host name、operator 固有 group policy。

config は current state の seed / desired declaration であり、runtime truth ではない。
config と live observation が矛盾した場合、read model は `desired_but_missing` /
`observed_elsewhere` / `stale` のような表示状態へ倒す。action permission は
side-effecting command の live preflight が決める。

#### repo-local config boundary (#12254)

Project Group / sublane placement の repo-local config は、runtime state の保存先では
なく **desired presentation declaration** である。config は review 可能な default /
preference を与え、current table / read model はそれを registry / runtime observation
と照合して projection を作る。

Config が管理してよい値:

- `groups`: public-safe `group_id`, display `label`, optional `sort_key`。
- `membership_rules`: workspace / repo label / lane pattern から group を選ぶ
  declarative rule。rule は built-in predicate 語彙だけを使い、module path /
  callable / script を持たない。
- `unit_overrides`: known workspace/lane の `preferred_group`, `position`,
  `pinned`, `hidden`, `preferred_projection`。
- `defaults`: unknown / missing group の display fallback、collapsed 初期値、
  degraded display wording。

Config が管理してはいけない値:

- target pane id / tmux session / live window geometry。
- role binding / workspace identity / lane identity の正本。
- handoff route / target resolver / direct send policy。
- owner approval / review / close / completion state。
- private project absolute path、private host name、operator 固有 color / layout
  default。

Runtime / registry から導出する値:

- workspace existence, workspace_id, canonical session, repo label:
  registry / workspace anchor。
- lane existence, role set, target availability, pane id, branch, cwd:
  live tmux / inventory / TargetRecord projection。
- observed freshness, stale reason, contradiction:
  runtime observation envelope / target observation cache。
- workflow / review / close status:
  Redmine durable records only。

#### conceptual schema

Parser / migration implementation remains a later code task, but #12262 fixes
the schema field contract that implementation must preserve:

> 実装メモ (#12263): 本 schema の **parser + sublane launch placement resolver** は
> `src/mozyo_bridge/domain/presentation_grouping.py` に実装済み
> (`PresentationGroupingConfig.from_record` / `resolve_launch_placement`)。
> launch context (workspace / project / lane facts) を desired Project Group へ
> 解決する display-only layer であり、handoff target / liveness / approval を持た
> ない。default / missing config は behavior-preserving、unknown group / unsupported
> version / authority-shaped key は fail-closed、identity conflict / desired-unit
> missing は visible degraded status へ倒す (下記 fallback matrix 準拠)。
> on-disk config (`.mozyo-bridge/config.yaml`) loader への結線は `#12190` で実装済み。
> current table への seed / migration は `#12304` で実装済み (`## 実装状況 (#12304)`)。

```yaml
presentation:
  version: 1
  project_groups:
    - group_id: "project:<public-label>"
      label: "<public display label>"
      sort_key: 10
  grouping:
    membership_rules:
      - when:
          repo_label: "<public repo label>"
        group_id: "project:<public-label>"
    unit_overrides:
      - workspace_id: "<workspace-id>"
        lane_id: "default"
        preferred_group: "project:<public-label>"
        position: 10
        pinned: false
        hidden: false
        preferred_projection: "cockpit_pane"
```

This shape is declarative metadata. It is not a plugin manifest, not an install
surface, and not a dynamic predicate language.

#### schema field contract (#12262)

`presentation.version`
: Integer schema version. Missing means `1` only if the implementation is still
  pre-version; once writer support exists, writers must emit it. Unsupported
  newer version is fail-closed for config application.

`presentation.project_groups[]`
: Declares known Project Groups. Allowed fields:
  `group_id` (required, stable portable key), `label` (required public-safe
  display label), `sort_key` (optional integer / string for display order),
  `collapsed` (optional bool default), `description` (optional public-safe text).
  It must not include target, pane, route, owner, review, close, credential, path,
  color theme, or private layout policy.

`presentation.grouping.membership_rules[]`
: Declarative display grouping rules. Allowed `when` predicates are public-safe
  facts that can be derived from registry / repo-local metadata without reading
  live pane identity: `workspace_id`, `repo_label`, `project_id`,
  `fixed_version_id`, `lane_id`, and `lane_prefix`. A rule result may set
  `group_id`, `position`, `pinned`, `hidden`, and `preferred_projection`. A rule
  must not name Python modules, callables, shell commands, dynamic predicates,
  target panes, or send routes.

`presentation.grouping.unit_overrides[]`
: Explicit desired display override for a known Unit. The selector is limited to
  `workspace_id` + `lane_id` (+ optional `host_id` for future host-aware
  projection). Allowed desired fields are `preferred_group`, `position`, `pinned`,
  `hidden`, `preferred_projection`, and `label_override` (public-safe only).
  `role_set` / `target` / `pane_id` / `session` / `window` are not configurable;
  they are runtime / registry facts.

`presentation.grouping.defaults`
: Display fallback only. Allowed fields are `missing_group`, `unknown_unit_group`,
  `collapsed`, `preferred_projection`, and `degraded_display`. Defaults must not
  invent workspace identity, lane identity, routing target, or workflow state.

Field ownership:

| field family | owner | notes |
| --- | --- | --- |
| group ids / labels / sort preferences | repo-local desired config | public-safe display only |
| workspace_id / repo_label / canonical session | registry / workspace anchor | config may reference, not define |
| lane_id / role set / pane availability | runtime observation / managed lane state | config may prefer display, not assert liveness |
| pane_id / tmux session / window / cwd / branch | TargetRecord / live tmux / inventory projection | never config truth |
| review / owner approval / close / completion | Redmine governed workflow | never config truth |

#### fallback matrix (#12262)

| condition | result |
| --- | --- |
| config missing | use implementation default grouping (`default` / repo label) and preserve routing behavior |
| `project_groups` empty | show ungrouped/default group; do not fail target discovery |
| unknown top-level field or unsupported version | ignore config and surface invalid config diagnostic |
| duplicate group id | invalid config |
| membership rule references unknown group | invalid config, unless implementation has explicit `unknown_group` degraded display |
| unit override references unknown workspace/lane | degraded display `desired_unit_missing` |
| live TargetRecord conflicts with override selector | degraded display `identity_conflict`; action preflight still decides |
| group has no live targets | display empty/stale group; do not fabricate targets |
| config wants hidden Unit with active target | display hidden preference and live availability separately; do not kill / detach / reroute |

#### validation / degraded display

Validation is fail-closed for authority leaks and degraded for ordinary display
drift:

- unknown top-level key / unknown field / unsupported version: invalid config,
  do not apply it.
- `target`, `pane`, `route`, `send`, `approval`, `review`, `close`, `owner`,
  `credential`, `secret`, `token`, `command`, `script`, `module`, `callable`,
  `import`, or similar boundary-shaped key/value: invalid config.
  - 値側の token scan 対象 (#12263 実装): identity / pointer key と diagnostic
    text、すなわち `group_id`、group 参照 (`preferred_group` / `missing_group` /
    `unknown_unit_group`)、`degraded_display`。これらは stable join key または
    operator 向け status channel に流れるため、上記 boundary token を値に持てない。
  - token scan しない public-safe free display prose: `label` / `description` /
    `label_override`。これらは author が public-safe と保証する表示用 prose であり、
    "Code Review" / "Closed projects" のような正当な語を含み得るため値側 token scan
    の対象外とする (key としては closed schema で依然 reject される)。
- duplicate `group_id`: invalid config.
- unknown `group_id` referenced by a rule/override: invalid config unless the
  implementation explicitly supports `unknown_group` degraded display.
- unknown workspace_id / lane_id in an override: degraded display
  (`desired_unit_missing`), not handoff failure by itself.
- workspace/lane identity conflict between config and live TargetRecord:
  degraded display (`identity_conflict`) and action-time preflight still decides
  any side effect.
- group membership derived from live geometry only: never accepted as config
  truth; show `observed_elsewhere` / `stale` instead.

Degraded display must be visible in the read model. It must not silently route
to another Unit or auto-create private grouping defaults.

#### migration stance

- Missing config preserves current behavior: group by public repo/workspace label
  as an implementation fallback, or show an ungrouped/default group. No routing
  behavior changes.
- Existing repo-local config versions are read with explicit `version` checks.
  Unsupported newer versions fail closed and leave live target discovery usable.
- Current tables (`cockpit_group_membership`, `projection_preferences`) may be
  seeded from config by an explicit command / migration, not by reading live
  geometry as truth.
- Migration from static config to home-scoped desired presentation current tables
  must be idempotent and non-destructive. It records source config version and
  does not write private path / host topology to public docs or scaffold.
- Public scaffold may include only generic example groups / schema comments.
  Project-specific default grouping belongs in the consuming repo or private
  operator config, not in `mozyo_bridge` defaults.

### live geometry boundary

tmux session / window / split tree は observed runtime geometry である。Project
Group / Unit の durable membership を live geometry から逆算して保存正本にしない。

扱ってよいもの:

- pane coordinate / column grouping / width。
- observed top-to-bottom / left-to-right order。
- drift diagnostics and repair preview。

扱ってはいけないもの:

- Unit がどの Project Group に属するかの正本。
- role / workspace / lane identity。
- routing / approval / close authority。

reconcile / rebalance / move のような future command は、desired presentation state
と live TargetRecord を照合して plan を出す。実行は preview / confirm gate を持ち、
private operator layout を OSS default にしない。

### projection_preferences

Unit ごとの preferred projection。`cockpit_pane` を primary としつつ、
`normal_window` compatibility を残す。

```sql
CREATE TABLE projection_preferences (
    unit_id TEXT PRIMARY KEY,
    preferred_projection TEXT NOT NULL, -- cockpit_pane | normal_window | future
    fallback_projection TEXT,
    updated_at TEXT NOT NULL,
    source_event_id INTEGER
);
```

この table は view selection の preference であり、handoff resolver の safety gate
ではない。handoff は live TargetRecord preflight を通す。

### managed_unit_events

`managed-events.sqlite` の append log と同等または将来統合される event/audit table。
初期実装では既存 `managed_events` を置き換えない。必要になった時だけ migration
task を切る。

```sql
CREATE TABLE managed_unit_events (
    id INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    unit_id TEXT,
    host_id TEXT NOT NULL DEFAULT 'local',
    workspace_id TEXT,
    lane_id TEXT,
    payload_json TEXT NOT NULL
);
```

### target_observations

latest runtime snapshot の cache。消えても live tmux から再観測できる。stale を明示
するため `observed_at` を必須にする。

```sql
CREATE TABLE target_observations (
    target_key TEXT PRIMARY KEY,        -- e.g. tmux:<host>:<pane_id>
    observed_at TEXT NOT NULL,
    host_id TEXT NOT NULL DEFAULT 'local',
    tmux_session TEXT,
    window_name TEXT,
    pane_id TEXT,
    role TEXT,
    role_source TEXT,
    confidence TEXT,
    ambiguous INTEGER NOT NULL DEFAULT 0,
    workspace_id TEXT,
    lane_id TEXT,
    repo_root TEXT,
    branch TEXT,
    view_kind TEXT
);
CREATE INDEX idx_target_observations_unit
  ON target_observations(host_id, workspace_id, lane_id, role);
```

`target_observations` は inventory cache であり、handoff の最終配送可否ではない。
preflight は live tmux pane existence / process / cwd / active split を再確認する。

## Query policy

CLI / UI は用途ごとに正本を選ぶ。

- handoff target selection: live tmux -> TargetRecord projection -> safety preflight
- cockpit desired layout: presentation current tables
- recovery / rebuild suggestion: current tables + managed event log
- workspace identity: registry -> workspace anchor -> path derivation
- completion / approval: Redmine

current table と live tmux が矛盾した場合:

- live tmux が handoff / liveness で勝つ
- current table は "desired but missing" / "stale observation" として表示する
- 自動で destructive reconcile しない

## Static file boundary

tracked file に残す:

- docs catalog
- rules / scaffold governance
- human-readable logic docs / runbooks
- minimal workspace anchor
- portable project defaults

DB に移す:

- cockpit group membership
- projection preference
- pinned / hidden / retired desired state
- current desired unit composition
- observation cache

tracked file に増やさない:

- unit ごとの JSON state
- target ごとの JSON state
- cockpit group membership YAML
- operator private lane policy

## Migration stance

1. 本 doc は schema boundary のみを固定する。
2. 実装時は read-only inspector / doctor から始め、write path は明示 task で切る。
3. 既存 `managed-events.sqlite` は即置換しない。
4. `registry.sqlite` schema に presentation state を追加しない。
5. private cockpit composition は product default に入れない。

## 実装状況 (#12304)

first slice を `src/mozyo_bridge/presentation_state.py` に実装した
(`fc-presentation-state-db-source` で本 doc に紐づく)。

実装済み:

- 置き場: `${MOZYO_BRIDGE_HOME}/presentation.sqlite`。`registry.sqlite` /
  `inventory.sqlite` とは別 DB (sibling store と同じ `*_path(home=None)` /
  `PRAGMA user_version` 規約)。
- current tables: `cockpit_group_membership` と `projection_preferences` を本 doc の
  schema どおり作成。加えて seed provenance を記録する
  `presentation_seed_provenance` table を持つ。
- seed / migration: 静的 repo-local `.mozyo-bridge/config.yaml` の `presentation`
  block (`PresentationGroupingConfig`) の **`unit_overrides` のみ** を current tables へ
  seed する (`seed_from_grouping_config`)。`preferred_group` (+ `position` / `pinned` /
  `hidden`) → membership、`preferred_projection` → projection preference。`unit_id` は
  `(host_id, workspace_id, lane_id)` から決定的に導出する public-safe join key。
- idempotent: content-comparing upsert。desired 内容が一致する row は `updated_at` も
  書き換えない。無変更 config の再 seed は完全な no-op。
- non-destructive: seed は insert / update のみで **delete しない**。config から消えた
  override の row や operator が手で足した row は残る。destructive auto reconcile は
  しない。
- source config version 記録: provenance に `source_config_version` を残す
  (#12304 受入条件)。
- read model: `classify_membership` が desired row を観測集合と突き合わせて
  `present` / `stale` / `desired_but_missing` の **表示状態** に倒す pure projection。
  routing / action 可否は決めない。
- fail-closed: 壊れた config は seed せず非 0 終了。未知 schema version / 壊れた DB は
  write path だけでなく **read path でも** `PresentationStateError` で停止する。`inventory`
  cache と異なり desired state を「空」とは読まず、auto-drop もしない (regenerable cache と
  異なる扱い)。`presentation show` / `seed` は例外を捕捉して非 0 + 明示 error を返す。
  存在しない DB file のみが正当な空 (未 seed) として扱われる。
- CLI: `mozyo-bridge presentation seed` (write、`--dry-run` で preview) と
  `presentation show` (read-only inspector)。

意図的に未実装 (schema design のまま):

- `membership_rules` の seed。rule は launch-time facts から group を導く live 解決で
  あり、`resolve_launch_placement` が launch 時に評価する。durable membership へ固めない
  (live geometry boundary)。
- `unit_desired_state` / `managed_unit_events` / `target_observations` table。adjacent
  だが本 slice では不要。必要になった時に別 task で切る。
- live tmux geometry からの membership 復元・reconcile / rebalance / move command。
  preview / confirm gate 付きの future command として本 doc が留保する。

不変条件 (本 slice で enforce):

- handoff / liveness / approval / close / routing / pane authority を持つ column は
  current tables に存在せず、seed も書かない。action 可否は action-time live preflight、
  workflow completion は Redmine 正本。

## Acceptance mapping

- `registry.sqlite` に runtime / projection state を混ぜない: 本 doc の
  `registry.sqlite` 禁止事項で固定。
- `inventory.sqlite` を liveness 正本にしない: `target_observations` を cache と定義。
- static docs / catalog / rules を DB 化しない: Static file boundary で固定。
- `workspace.json` は minimal anchor のまま維持: workspace identity 層に限定。
- 実装分割可能な schema doc: To-Be tables と migration stance を明記。
