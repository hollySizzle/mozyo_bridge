# Pane-Centric Cockpit Semantics

Redmine #11999。iTerm2 control mode、VS Code terminal integration、tmux
plugin、operator の手動 tmux 操作など、`mozyo-bridge` 以外の actor が
tmux session / window / pane / layout を触る前提で、cockpit の意味構造を
どこに置くかを定義する。

## 結論

`mozyo-bridge` は tmux の session / window / split tree に意味を持たせすぎない。

意味は次の層に分ける。

```text
durable identity        -> registry / workspace anchor / lane id / role
runtime target          -> live tmux pane_id + pane user options + cwd/process preflight
desired presentation    -> unit/presentation current tables (future)
live geometry           -> observed tmux layout, drift し得る
display label           -> pane title / border / prefix / iTerm UI, projection only
```

`pane_id` は handoff 可能な runtime target として重要だが、durable source of
truth ではない。pane が消えたら消える。長期 identity は workspace / lane /
host / role に置く。

## Why

tmux を操作する actor は `mozyo-bridge` だけではない。

- iTerm2 control mode
- VS Code integrated terminal / extension
- terminal app の tmux integration
- operator の手動 `join-pane` / `move-pane` / `swap-pane` / resize
- tmux restore plugin
- remote SSH 上の tmux
- agent 自身の terminal action

これらが live layout を変更しても、workflow routing / ownership / lane identity
が壊れてはいけない。

## Identity Contract

### Primary semantics

primary semantics は machine-readable marker と durable registry から読む。

```text
@mozyo_workspace_id
@mozyo_lane_id
@mozyo_agent_role
@mozyo_managed
workspace registry
workspace anchor
repo / cwd preflight
```

handoff / target resolution は、最終的に live pane に対して process / cwd / repo /
role / workspace / lane preflight を通す。

### Compatibility semantics

normal local `mozyo` session では、window name `claude` / `codex` を compatibility
fallback として使ってよい。ただし `role_source=window_name` と明示する。

cockpit では window name は layout attribute であり、role identity ではない。

### Display-only semantics

次は display / discovery hint であり、authority ではない。

- tmux session name prefix
- tmux window name
- pane title
- pane border label
- iTerm window / tab / split
- VS Code terminal label
- `mozyo:` display prefix

prefix があるだけで managed / safe / targetable と判定してはならない。prefix が
無いだけで unmanaged と断定してもならない。

## Desired Presentation Contract

operator が意図する cockpit composition は live split tree ではなく desired
presentation state として扱う。

代表的な state:

```text
group_id
unit_id
position
width_weight
pinned
hidden
preferred_projection
```

これは `unit-presentation-state-db.md` の `cockpit_group_membership` /
`projection_preferences` の責務である。実装済みではないが、今後の column move /
rebalance / reconcile はこの table 境界に従う。

## Live Geometry Contract

tmux の `window_layout` / pane coordinate / split tree は observed state である。

正本にしてよいもの:

- 今の pane の矩形
- 現在の split tree
- resize 後の実寸

正本にしてはいけないもの:

- Unit の所属
- role
- workspace / lane identity
- owner / review / completion state
- long-term column order policy

live geometry と desired presentation が矛盾した場合:

- handoff / liveness は live tmux を見る
- grouping / order / preferred width は desired presentation を見る
- 自動 destructive reconcile はしない
- drift として表示し、明示 command で reconcile / rebuild する

## Pane Move / Swap Policy

外部 tool の raw pane operation は許容する。ただし `mozyo-bridge` の意味構造を
それに依存させない。

### Raw `move-pane` / `join-pane`

`move-pane` / `join-pane` は destination pane を split し、source pane をそこへ
差し込む。これは project column を知っている操作ではない。

したがって raw move は cockpit の project-column intuition を壊し得る。

### Raw `swap-pane`

`swap-pane` は pane の位置入れ替えに近く、split tree 破壊は少ない。ただし、
Codex / Claude の 2 pane を Unit column としてまとめて動かす意味は持たない。

### mozyo column operation

将来の safe operation は raw pane ではなく Unit / column を対象にする。

候補:

```text
mozyo cockpit move --unit <unit> --before <unit>
mozyo cockpit move --unit <unit> --after <unit>
mozyo cockpit rebalance --session <group>
mozyo cockpit reconcile --session <group>
mozyo cockpit doctor-geometry --session <group>
```

これらは desired presentation state と live TargetRecord を照合して plan を出す。
実行は preview / confirm gate を持つ。

## Display Prefix Policy

他 tmux integration tool との混線を減らすため、display label に `mozyo` marker を
出すのは有効である。

推奨:

```text
pane title:  mozyo:<repo-label>:<role>
pane border: [mozyo <repo-label> <role> <attention>]
window status: mozyo <attention>
```

制約:

- prefix は短くする。
- private project path を出さない。
- secret-shaped value を出さない。
- prefix は projection であり authority ではない。
- prefix が消えても identity / handoff が壊れない。
- prefix が偽装されても managed / safe と判定しない。

authority は user option / registry / preflight に置く。

## Drift Detection

初期 drift detection は read-only でよい。

検出候補:

- same Unit の Codex / Claude pane が同じ x-range column に無い。
- expected project columns が row-wise split に変わっている。
- desired `position` と observed left-to-right order が違う。
- desired `width_weight` と observed width が大きく違う。
- pane marker はあるが display label が missing / stale。
- display prefix はあるが user option marker が無い。

drift があっても handoff を自動禁止しない。handoff safety は live target
preflight で判断する。drift は operator UX / recovery issue である。

### Geometry Diagnosis (`cockpit doctor-geometry`)

Redmine #12131 で上記 drift detection の最初の read-only 入口を実装した:
`mozyo cockpit doctor-geometry [--session <group>] [--json]`。live tmux の
cockpit window から各 pane の identity option (`@mozyo_workspace_id` /
`@mozyo_agent_role` / `@mozyo_lane_id`) と observed geometry (`pane_left` /
`pane_top` / `pane_width` / `pane_height`) を読み、Unit
(`workspace_id` + `lane_id`) と vertical column (x-range overlap) を導出して
診断する。

実装は pure domain (`diagnose_cockpit_geometry`) + read-only tmux reader で、
**tmux layout を一切変更しない**。repair / rebalance / move は本入口の scope 外
(US #12130 が後続 issue に分割)。出力 finding code:

- `missing_codex` / `missing_claude` — Unit に片側 agent しか無い。
- `role_less_pane` — cockpit pane が role/workspace marker を欠く
  (#12130 の手動復旧 `%1106` ケース)。
- `unit_column_split` — 同一 Unit の codex/claude が同じ column に収まらない。
- `mixed_unit_column` — 一つの vertical column に複数 Unit が同居する。
- `narrow_pane` — column 幅が median から極端に外れた width imbalance
  (advisory notice; `ok` を倒さない)。

severity は `warning` (構造 drift) と `notice` (width imbalance) の二段で、
`warning` が一つでもあれば `ok=false` / exit 1、無ければ exit 0。これは
`doctor` の exit 規約と揃え、JSON は exit code に関わらず stdout へ出す。
diagnosis は **observed geometry であり identity authority ではない** —
どの Unit に属するかを再決定せず、handoff も禁止しない。private absolute path は
出力に含めない (geometry reader は cwd / repo root を読まない)。

### Peer Adopt (`cockpit peer-adopt`)

Redmine #12133 で上記 drift detection に対する最初の安全な **repair** slice を
実装した: `mozyo cockpit peer-adopt --pane %id --unit <workspace>/<lane>
--role <claude|codex> [--dry-run] [--json] [--confirm]`。`doctor-geometry` が
報告する `role_less_pane` と `missing_claude` / `missing_codex` の組を入力に、
role-less pane を既存 Unit の **missing peer role** として採用する。

採用は **pane option identity binding に限定** する — `@mozyo_workspace_id` /
`@mozyo_agent_role` / `@mozyo_lane_id` (+ 任意の `@mozyo_lane_label`) を対象 pane
に bind し、pane title を整える。pane の move / kill / split / join / layout
mutation / rebalance は一切行わない (それらは別 issue)。Unit grouping は
pane option / workspace / lane / role を正本のままにし、observed geometry を
identity authority に昇格させない。

apply は **fail-closed**。pure planner (`plan_peer_adopt`) が以下の guard を
すべて通過し、かつ `--confirm` が指定された場合のみ mutation する。`--dry-run` /
`--json` および `--confirm` なしの実行は preview のみで何も変更せず、mutable
tmux server を要求しない。

- cockpit window が存在する。
- `--role` が agent role (`claude` / `codex`)。
- `--pane` が cockpit window 内の pane を指す。
- その pane が **role-less** である (既に identity を持つ pane は決して
  再 home しない)。
- 宛先 Unit (`workspace_id` + `lane_id`) が既に存在する (peer-adopt は missing
  peer を埋めるのであって、新規 Unit を bootstrap しない)。
- Unit が **ちょうど** その role を欠き、peer role を既に持つ (missing peer が
  ちょうど一つ、present peer に anchor される)。
- 候補 pane の cwd が resolve できる場合、その workspace / lane が宛先と矛盾
  しない (resolve 不能な cwd は "unknown" として許容、矛盾する cwd は block)。
- 候補 pane の foreground process が **別** agent role を示唆しない。

apply executor は identity bind を小さな transaction として扱い、途中失敗時は
bind 済み option を unset して pane を role-less 状態へ戻す (half-bound drift を
新たに作らない)。これにより **cross-session / cross-lane Claude direct boundary を
弱めない** — checkout が宛先 Unit と矛盾する pane は採用されず、黙って
re-home されることはない。preflight が読む cwd は workspace_id / lane へ resolve
した id だけを記録に残し、private absolute path は output / record に出さない。
apply 後は `doctor-geometry` と `agents targets` で missing-peer / role-less
finding の解消を smoke する。

### Width Rebalance (`cockpit rebalance`)

Redmine #12135 で width skew の repair 入口を実装した:
`mozyo cockpit rebalance [--session <group>] [--dry-run | --json] [--confirm]`。
既存 live cockpit が manual `resize-pane` / #11854 以前の append / 外部 tmux
integration で column 幅 skew (US 対象の `56 / 50 / 99 / 69` 等) を抱えた場合に、
observed column を **equal fair-share 幅** へ戻す。

実装は pure domain (`parse_window_layout` / `top_level_columns` /
`build_cockpit_rebalance_plan`) + confirm-gated executor で、column model は
**tmux `window_layout` tree の top-level cell** を正本にする。`doctor-geometry`
の x-range overlap clustering とは **別物** であり、それを再利用しない。理由:
x-cluster は構造 drift した cockpit (top-level cell に複数 Unit が nested する
2x2 grid など) を clean column と誤認し、cluster 代表 pane を `resize-pane` すると
内側 sub-split 境界を動かして layout を壊す (#12135 live apply gap の原因)。layout
tree だけが resizable boundary の正本なので、それを parse して top-level cell から
column を導出し、`resize-pane -x` の plan を出す。preview-first / confirm gate:
`--dry-run` / `--json` と bare command は非変更の preview、`--confirm` のときだけ
plan を適用する。境界:

- column model は `window_layout` top-level cell。**identity authority ではない**
  (Live Geometry Contract どおり observed state)。Unit 所属 / role / lane は
  pane option / registry のまま。
- target は equal fair-share。`width_weight` desired-presentation table
  (`unit-presentation-state-db.md`) は未実装のため、interim target は
  `even_column_share` と同じ equal fair-share。table 実装後はそちらへ寄せる。
- column 幅のみを `resize-pane -x` で変える。`set-option` は出さない —
  identity pane option (`@mozyo_workspace_id` / `@mozyo_agent_role` /
  `@mozyo_lane_id`) は不変更。
- `select-layout even-horizontal` を実行しない — 各 column の Codex/Claude
  vertical split を flatten しない (#11807 regression を避ける)。
- top-level cell が clean な full-width split でない (= 横 sub-split を抱える
  nested 2x2 / mixed-Unit の layout-tree drift) 場合は **fail-closed**。width
  resize はその構造を直せないので、layout-tree structural reconcile (#12136
  scope) へ委譲する。`doctor-geometry` の x-cluster diagnosis はこの nested cell を
  `ok` と報告し得る (検出しない) 点に注意。`missing_claude` / `role_less_pane`
  単体は rebalance failure 扱いしない (#12133 scope) — clean な full-width column
  なら resize する。
- tolerance 内 (既に fair) または column 2 未満なら benign no-op。
- private absolute path は出力に含めない。

### Structural Reconcile (`cockpit reconcile`)

Redmine #12136 で structural layout-tree drift の repair 入口を実装した:
`mozyo cockpit reconcile [--session <group>] [--ratio N] [--dry-run | --json] [--confirm]`。
#12133 peer-adopt 後も残る、2 つの Unit column が 1 つの tmux top-level cell に
nested する 2x2 grid drift (live `[ {%1104|%953}, {%1106|%954} ]`) を、各 Unit が
独立した clean column になるよう flatten する。これにより #12135 rebalance が
fail-closed せず動けるようになる。

実装は pure domain (`parse_window_layout` / `build_cockpit_reconcile_plan` /
`build_unit_columns_layout` / `layout_checksum`) + confirm-gated executor。
**order-preserving** な手法を採る: `select-layout` は layout 文字列内の pane id を
無視し live pane を **pane 順** で leaf に割り当てる (scratch 確認済み) ため、まず
`swap-pane` で live pane 順を column-major (Unit0 codex, Unit0 claude, Unit1 codex,
…) に揃え、続いて checksum-valid な `select-layout` を 1 回適用して各 Unit を
既存の左右順のまま clean column に並べる。preview-first / confirm gate:
`--dry-run` / `--json` と bare command は非変更 preview、`--confirm` のみ適用。境界:

- Unit identity は pane option (`@mozyo_workspace_id` / `@mozyo_lane_id` /
  `@mozyo_agent_role`) から読む。geometry から identity を推測しない。geometry
  (`pane_left`) は Unit を左右に **並べる順序** にのみ使う。
- **pane kill しない**。`swap-pane` / `select-layout` は live pane を move/relayout
  するだけで identity option はそのまま乗る (re-stamp 不要)。
- fail-closed: unidentified (role-less) pane (identity adoption = `cockpit adopt`
  flow scope)、Unit 内の同一 role 重複 pane、1 Unit が複数 top-level cell に
  またがる split、parse 不能 layout のいずれかで commands を出さず blocked。
- residual risk: swap 後に `select-layout` が失敗した場合、pane は順序が変わるのみ
  (kill なし) で recoverable。executor は fail-fast し「no pane killed / re-run」を
  返す。再実行で live 順から再 sort する。
- column 幅は valid な even fair share に設定する (`select-layout` が幅を要求する
  ため)。細かな幅調整 / weight は #12135 rebalance / 将来 scope。
- private absolute path は出力に含めない。

## Public / Private Boundary

OSS default に入れてよいもの:

- generic state names
- generic display prefix
- desired table schema boundary
- read-only drift detection
- preview-first reconcile command contract

入れないもの:

- private project grouping policy
- operator 固有 color / order / shortcut
- iTerm profile / VS Code setting の private defaults
- business dashboard composition
- private path or private repository topology

## Follow-up Implementation Split

本 doc は仕様固定のみ。実装は別 issue に分ける。

候補:

1. `cockpit doctor-geometry`: live layout drift の read-only diagnosis。
   **実装済み (Redmine #12131)** — `### Geometry Diagnosis` を読む。
1b. `cockpit peer-adopt`: role-less pane を既存 Unit の missing peer として
   採用する最初の repair slice。**実装済み (Redmine #12133)** —
   `### Peer Adopt` を読む。
2. `cockpit rebalance`: current live columns の width を desired / fair share に戻す。
   **実装済み (Redmine #12135)** — `### Width Rebalance` を読む。
3. `cockpit reconcile`: nested 2x2 top-level cell drift を per-Unit column へ
   flatten する structural repair。**実装済み (Redmine #12136)** —
   `### Structural Reconcile` を読む。
4. `cockpit move`: Unit column reorder primitive。
5. `presentation-state DB`: `cockpit_group_membership` current table。
6. display prefix projection: pane title / border label に `mozyo:` prefix を追加。
7. drift-safe iTerm/WebViewer controls: raw pane move ではなく Unit move を呼ぶ UI。

## Non-Goals

- live tmux layout を唯一正本にすること。
- `mozyo:` prefix を managed 判定に使うこと。
- external tmux tools を禁止すること。
- iTerm2 専用の layout semantics を core に hard-code すること。
- DB state だけで live pane existence を判断すること。

## Verification

実装時は次を pin する。

- role / workspace / lane は pane option / registry primary。
- window / session / display prefix は fallback または projection。
- geometry drift は attention / warning になっても routing authority にならない。
- reconcile / move / rebalance は preview-first。
- private path / operator policy を tracked docs に入れない。
