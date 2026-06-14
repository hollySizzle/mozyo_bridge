# Workspace Anchor / Project Defaults Migration

Redmine #11905 / #11910。`.mozyo-bridge/workspace.json` と
`.mozyo-bridge/workspace-defaults.yaml` の名前が責務を誤解させるため、rename
方針と互換 migration を固定する設計正本。

本 doc は migration design である。runtime compatibility は Redmine #11920 /
#11921 で実装済み。実装が固定した runtime contract:

- read path (resolution / cockpit / session naming) は新名を優先し、旧名へ
  fallback する。両名が存在しても新名が authoritative なので read は壊れない。
  shared helper は `src/mozyo_bridge/shared/name_compat.py`。
- mutating / explicit command (`workspace register`, `workspace-defaults`,
  `runtime-config install`) は both-exist で fail closed し、operator に旧名の
  削除を求める (silent merge しない)。旧名のみのときは command level で
  deprecation warning を出す (hot read path は静か)。
- 新規 write は新名のみ (`workspace-anchor.json` / `project-defaults.yaml`)。
  `workspace register` が旧名のみの workspace を register するときは identity を
  旧 anchor から復元して新名へ書き、旧 anchor は残したまま削除を促す note を
  返す (本実装では旧 fallback file を削除しない)。
- generated `redmine-defaults.md` の source 参照は実際に読んだ入力ファイル名を
  反映する (新名 workspace は新名、旧名 workspace は旧名を表示)。

## 決定

rename する。ただし file を増やすのではなく **責務に合う名前へ移す**。

```text
.mozyo-bridge/workspace.json
  -> .mozyo-bridge/workspace-anchor.json

.mozyo-bridge/workspace-defaults.yaml
  -> .mozyo-bridge/project-defaults.yaml
```

旧名は read fallback と migration source として扱う。新規 write は新名のみ。
通常運用で旧名・新名を二重に書き続けない。

## 理由

### workspace.json

現名は「workspace の全状態」を入れてよい file に見える。しかし実態は
workspace identity recovery anchor である。

保持してよい:

- schema_version
- workspace_id
- canonical_session
- project_name
- created_at / updated_at

保持してはいけない:

- lane state
- cockpit group membership
- projection preference
- pane/window/process/cwd liveness
- private operator policy

`workspace-anchor.json` は「registry 喪失時に identity を復元する最小 seed」である
ことを名前で示す。

### workspace-defaults.yaml

現名は workspace runtime defaults に見える。しかし実態は project-level defaults、
特に Redmine default project / generated output 設定である。

保持してよい:

- default Redmine project
- verification metadata
- generated output declarations

保持してはいけない:

- lane policy
- cockpit composition
- runtime liveness
- private business workflow

`project-defaults.yaml` は「workspace 内 project governance defaults」であることを
示す。runtime state の置き場ではない。

## Compatibility story

### Reader priority

実装後の read path は次の順序にする。

```text
new name exists -> read new name
old name exists -> read old name with deprecation warning
both exist      -> fail closed or doctor-red depending on command mutability
neither exists  -> current fallback
```

`both exist` は silent merge しない。どちらが正本か不明になるため、operator に
migration / deletion を求める。

### Writer policy

新規 write は新名のみ。

- `workspace register` writes `.mozyo-bridge/workspace-anchor.json`
- smart `init` guarded registration writes `.mozyo-bridge/workspace-anchor.json`
- `workspace-defaults` renderer reads `.mozyo-bridge/project-defaults.yaml`
- generated snippet remains `.mozyo-bridge/redmine-defaults.md` unless a separate
  task changes it

旧名が存在して新名が無い場合、write command は原則 explicit migration を促す。
自動 rename は破壊的になり得るため、implementation task で `--migrate` 等の明示
flag を設計する。

### Doctor / inspect

doctor / inspect は次を表示する。

- old-only: warning, migration available
- new-only: ok
- both: error or red drift, manual resolution required
- neither: current unregistered / unconfigured state

doctor は file を勝手に書き換えない。migration command を提示するだけにする。

### Migration command shape

実装時の案:

```bash
mozyo-bridge workspace migrate-names --repo <root> --confirm
```

動作:

1. old file の schema と secret safety を validate。
2. new file が存在しないことを確認。
3. old -> new へ rename。
4. generated output は必要に応じて existing renderer で再生成。
5. 結果を JSON / text で報告。

`--confirm` 無しでは preview のみ。rename は destructive 寄りなので、implicit に
走らせない。

## Implementation checklist

実装 task は少なくとも次を同一 scope で扱う。

### workspace anchor

- `ANCHOR_RELATIVE` の新名化
- old anchor read fallback
- both-exist detection
- register write path new-only
- smart init write path new-only
- workspace inspect / doctor warning
- tests:
  - new-only read
  - old-only fallback warning
  - both-exist fail closed
  - register writes new name
  - old anchor migration preview / confirm

### project defaults

- input path new名化
- old defaults read fallback
- both-exist detection
- renderer write/check path update
- `.mozyo-bridge/redmine-defaults.md` generated comment update
- docs / skill reference updates
- tests:
  - new project-defaults render
  - old workspace-defaults fallback warning
  - both-exist fail closed
  - generated markdown references new source
  - secret rejection remains unchanged

### scaffold / docs

- scaffold generated files use new names
- catalog / file conventions updated
- examples updated
- release / sync checks updated if needed

## Non-goals

- Do not store lane / cockpit / projection state in anchor/defaults.
- Do not create a second tracked state file beside the old one as normal state.
- Do not migrate private cockpit composition into OSS defaults.
- Do not use registry.sqlite as a dumping ground for presentation state.
- Do not remove old read fallback in the same commit that introduces new names.

## Public / private boundary

The renamed files are public generic primitives:

- workspace identity recovery anchor
- project default configuration

They must not encode private lane naming, private cockpit grouping, internal project
composition, credential, token, cookie, or personal path policy.

## Rollout recommendation

1. Docs-only decision (this task).
2. Runtime compatibility implementation:
   - new read/write names
   - old read fallback
   - both-exist diagnostic
   - tests
3. Scaffold update.
4. Optional explicit migration command.
5. After at least one release cycle, decide whether old-name fallback remains or
   becomes stronger warning. Do not remove fallback until downstream scaffolded
   repos have a documented migration path.

## Acceptance mapping

- rename decision: yes, rename by responsibility.
- compatibility story: old read fallback, new write, both-exist fail/doctor,
  explicit migration.
- no file増殖: normal write path writes new names only; no dual-write.
- lane / cockpit / projection state: explicitly excluded from anchor/defaults.
