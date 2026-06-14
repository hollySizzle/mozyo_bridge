# Unit / Target / Projection model

Redmine #11905 / #11906。`mozyo cockpit`、通常 local `mozyo` session、
cross-project / multi-worktree 運用を、tmux / iTerm の表示形状ではなく
同じ unit target model で扱うための設計正本。

## 結論

```text
Canonical model: TargetRecord / UnitRecord
Recommended projection: cockpit_pane
Supported compatibility projection: normal_window
```

`normal local session` は即退役しない。compatibility maintenance mode として
維持する。ただし新しい multi-lane / cross-project / coordinator /
projection-state 機能は `cockpit_pane` を primary projection として進める。

「どちらでも同格」にはしない。handoff / discovery / docs の判断語彙は
`TargetRecord` / `UnitRecord` へ寄せる。

## Design-first gate

`TargetRecord` / `UnitRecord` は cockpit / normal local / cross-project /
DB state 境界にまたがるため、実装を先に走らせると resolver、handoff、docs、
state store が別々の語彙で育つ。

したがって #11905 配下では、次の順序を守る:

1. Redmine に意思決定と経緯を残す。
2. repo-local logic doc に現在の設計正本を固定する。
3. catalog に登録し、`docs resolve` / `audit-impact` の導線へ乗せる。
4. その後に #11907 以降の実装へ進む。

Redmine journal は意思決定の履歴であり、repo-local logic doc は実装者と監査者が
読む現在の正本である。片方だけでは足りない。

## 用語

### Unit

作業単位。人間、coordinator、Redmine gate が扱う logical grouping である。

```text
Unit = workspace + lane + project/governance context + role set
```

例:

- mozyo_bridge main lane
- mozyo_bridge issue lane
- 別 project の cockpit column

Unit は handoff の直接配送先ではない。handoff する場合は Unit から role を選び、
最終的に Target へ解決する。

### Target

実際に送れる配送先。live tmux 上の pane を中心に、host、runtime、
role、workspace/lane identity を束ねる。

```text
Target = host + tmux runtime + pane_id + role + workspace/lane identity
```

handoff は最終的に Target に対して行う。pane_id だけを信じず、process /
cwd / repo / role / workspace / lane の preflight を通す。

### Projection

Unit / Target の見せ方。routing / governance の正本ではない。

代表例:

- `normal_window`: workspace-scoped session の `claude` / `codex` window
- `cockpit_pane`: cockpit group session の `cockpit` window 配下 pane
- future `webviewer_unit`: event / inventory projection による表示

## session / window / pane の扱い

```text
session = runtime group / view attribute
window  = view / compatibility attribute
pane    = runtime target identity
```

### session

通常 local `mozyo` では workspace の canonical session が使われる。cockpit では
named cockpit session が group として使われる。remote SSH host の tmux session は
local host の tmux session と物理的に混ぜない。

`canonical_session` は workspace identity に近い安定名として registry に持ってよい。
一方で、今その tmux session が存在するかは live tmux runtime の正本である。

### window

window name は primary role identity ではない。

- normal local では `claude` / `codex` window が role fallback になる。
- cockpit では window name が `cockpit` になるため、window name だけでは role を
  判定できない。

window name fallback を使った場合は `role_source=window_name` として明示する。

### pane

pane は handoff の最終配送先である。ただし pane_id は runtime identity なので、
DB / docs / Redmine の durable identity ではない。

preflight では少なくとも次を確認する:

- pane が存在する
- foreground process が receiver allowlist に入る
- cwd / repo が期待と合う
- role が期待と合う
- workspace_id / lane_id が selector と合う
- ambiguous ではない

## Resolver priority

1. explicit pane target が指定されている場合:
   - pane existence / process / cwd / repo preflight を確認する
   - pane option role / workspace / lane があれば primary とする
2. tmux pane option:
   - `@mozyo_agent_role`
   - `@mozyo_workspace_id`
   - `@mozyo_lane_id`
3. workspace registry / anchor / repo facts:
   - workspace identity
   - canonical session
   - git branch / common dir / checkout facts
4. window name fallback:
   - normal local compatibility のためだけに使う
   - `role_source=window_name` と明示する
5. ambiguous / missing:
   - fail closed または explicit target を要求する

## Projection policy

### cockpit_pane

recommended projection。multi-lane / cross-project / coordinator / sublane
運用の primary UX とする。

特性:

- session は cockpit group
- window は cockpit layout
- pane が role / lane / workspace を持つ
- role_source は pane option が primary
- workspace/lane は pane option と registry / checkout facts で確認する

### normal_window

supported compatibility projection。即退役せず、compatibility maintenance mode
として維持する。

特性:

- session は workspace canonical session
- window は `claude` / `codex`
- role_source は window name fallback になり得る
- safety / compatibility bug は直す
- new multi-lane / cross-project UX を無理に同等移植しない

### cross-project cockpit

同じ cockpit group に別 project unit を載せてよい。ただしそれは display grouping
であり、routing / governance の正本ではない。

cross-project handoff は target project の Codex Target を gateway として通す。
別 project の Claude へ direct send しない。

## TargetRecord / UnitRecord

### TargetRecord

JSON projection の概念例:

```json
{
  "host": {"id": "local", "label": "local", "kind": "local"},
  "runtime": {
    "provider": "tmux",
    "session": "mozyo-cockpit",
    "window": "cockpit",
    "pane_id": "%953",
    "process": "codex",
    "cwd": "<local path>"
  },
  "identity": {
    "workspace_id": "...",
    "lane_id": "default",
    "role": "codex",
    "role_source": "pane_option",
    "confidence": "strong",
    "ambiguous": false
  },
  "repo": {
    "label": "mozyo_bridge",
    "branch": "main"
  },
  "view": {
    "kind": "cockpit_pane",
    "group": "mozyo-cockpit",
    "active": true
  }
}
```

JSON は CLI / API projection であり、保存正本ではない。TargetRecord を unit /
target ごとの JSON file として永続化しない。

### UnitRecord

UnitRecord は TargetRecord の grouping である。

```json
{
  "unit_id": "unit:<host>:<workspace_id>:<lane_id>",
  "workspace_id": "...",
  "lane_id": "default",
  "repo_label": "mozyo_bridge",
  "branch": "main",
  "targets": {
    "codex": "tmux:<host>:<pane_id>",
    "claude": "tmux:<host>:<pane_id>"
  },
  "governance": {
    "ticket_system": "redmine",
    "owner_facing_role": "codex"
  }
}
```

UnitRecord は作業単位を表す。handoff は UnitRecord から role を選んで
TargetRecord へ落としてから行う。

## State boundary

```text
workspace identity      -> registry.sqlite + minimal workspace anchor
runtime liveness        -> live tmux
inventory projection    -> inventory.sqlite cache + JSON output
desired presentation    -> DB current tables (future)
desired event history   -> managed-events.sqlite / event tables
workflow completion     -> Redmine journal/status
```

### Static file に残すもの

- docs catalog
- rules
- scaffold governance
- generated guard docs
- project defaults
- minimal workspace anchor
- human-readable docs / runbooks / specs

### DB に寄せるもの

- mutable desired state
- cockpit group membership
- projection preferences
- pinned / hidden / retired
- target observation cache

### DB に寄せないもの

- live liveness
- pane existence
- foreground process
- cwd truth
- Redmine review / owner approval / completion

## File naming direction

現状:

```text
.mozyo-bridge/workspace.json
.mozyo-bridge/workspace-defaults.yaml
```

責務上のより良い名前:

```text
.mozyo-bridge/workspace-anchor.json
.mozyo-bridge/project-defaults.yaml
```

rename は互換 migration が必要である。旧 file read fallback、新規 write、doctor
warning、scaffold / docs / tests 更新を設計してから行う。file を増やさず rename を
基本方針とする。

## Anti-patterns

- `window_name == role` を primary identity に戻す。
- cockpit resolver と normal resolver を別々に育てる。
- normal local に cockpit と同じ multi-lane UX を無理に移植する。
- normal local を silent deprecated にして壊れたまま放置する。
- cockpit layout を core identity にする。
- unit / target ごとの JSON file を保存正本として増やす。
- `workspace.json` に lane / cockpit / projection state を足す。
- `registry.sqlite` に pane / window / process / cwd を入れる。
- `inventory.sqlite` を liveness 正本にする。
- Redmine gate / completion を mozyo DB へ複製して正本化する。
- private cockpit composition / operator policy を OSS default に入れる。

## 実装順序

1. 本 doc で model / schema / resolver priority を固定する。
2. `agents targets` を TargetRecord canonical projection に拡張する (#11907)。
3. handoff / pane resolver を TargetRecord 経由へ寄せる (#11908)。
4. desired / presentation state の DB current table 境界を設計する (#11909)。
5. workspace anchor / project defaults の rename migration を判断する (#11910)。
6. cross-project cockpit smoke / runbook を定義する (#11911)。

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --repo . --check`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
