# Unit Presentation State DB Boundary

Redmine #11905 / #11909。`UnitRecord` / `TargetRecord` を cockpit や
future projection で扱うときの **desired / presentation state** を、tracked
static file ではなく DB current table として扱うための To-Be schema 境界。

本 doc は schema design であり、runtime 実装ではない。実装時は別 task で migration、
CLI/API、tests を切る。

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

## Acceptance mapping

- `registry.sqlite` に runtime / projection state を混ぜない: 本 doc の
  `registry.sqlite` 禁止事項で固定。
- `inventory.sqlite` を liveness 正本にしない: `target_observations` を cache と定義。
- static docs / catalog / rules を DB 化しない: Static file boundary で固定。
- `workspace.json` は minimal anchor のまま維持: workspace identity 層に限定。
- 実装分割可能な schema doc: To-Be tables と migration stance を明記。
