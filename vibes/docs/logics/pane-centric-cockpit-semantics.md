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
2. `cockpit rebalance`: current live columns の width を desired / fair share に戻す。
3. `cockpit move`: Unit column reorder primitive。
4. `presentation-state DB`: `cockpit_group_membership` current table。
5. display prefix projection: pane title / border label に `mozyo:` prefix を追加。
6. drift-safe iTerm/WebViewer controls: raw pane move ではなく Unit move を呼ぶ UI。

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
