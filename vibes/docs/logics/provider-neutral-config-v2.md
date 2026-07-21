# provider 中立 config schema v2（role-canonical topology + named runtime profile）

Redmine #14148。repo-local `.mozyo-bridge/config.yaml` の schema を、provider brand が
workflow / topology / launch authority へ漏れる v1 から、role を正本とし provider を
trusted adapter 属性へ閉じ込める v2 へ移行する設計・運用正本。中立ポリシーの上位設計は
`modular-config-driven-refactor.md` / `plugin-ready-adapter-boundary.md`、durable role
authority は `herdr-default-lane-workflow-role-authority.md`、mzb1 identity は
`herdr-native-identity.md` を参照する。本書はそれらを置き換えず、config surface の schema /
migration / lifecycle / runtime 反映境界を定義する。

## 1. 原則

- **role が authority、provider は交換可能な実行 adapter 属性**。どの provider がどの role を
  走らせるかは config が決めるが、workflow gate / route / approval authority は provider へ
  依存しない。provider を差し替えても gate は変わらない。
- **provider brand は named runtime profile の中だけに現れる**。profile の `provider` field は
  built-in adapter id（registered adapter profile）への参照であり、任意の実行ファイル / argv[0]
  / path を指せない（trusted executable boundary）。

## 2. schema v2（`agents` block）

```yaml
version: 2
agents:
  profiles:                     # named runtime profile: provider + 起動 argv を持つ唯一の面
    coordination: { provider: codex,  launch_argv: { default: [...], sublane: [...] } }
    implementation: { provider: claude, launch_argv: { sublane: ["--model", "claude-opus-4-8"] } }
  roles:                        # role -> profile 名。role = authority、profile = adapter 属性
    coordinator: coordination
    implementer: implementation
```

- `profiles.<name>.provider` は `agent_provider_ids()`（packaged trusted registry）に登録された
  adapter id のみ。未登録は fail-closed。`launch_argv` は `lane_class -> [tokens]`（`default` /
  `sublane`）で、#13425 の token / reserved-flag 規則を再利用する。
- `roles.<role>` は closed な `WORKFLOW_ROLES` の role を profile 名へ束ねる。宣言しない role は
  built-in default topology（下記）へ解決する。
- v1 の `provider_binding`（role→provider）/ `agent_launch`（provider→lane_class→argv）/ legacy
  `sublane_claude_model` は v2 では **`agents` へ畳まれ、v2 record には書けない**（cross-version
  key gate で fail-closed）。v1 と v2 の block 集合は disjoint。

### built-in default topology（role-canonical、一箇所）

default（`agents` 無し）は role-canonical な canonical default topology へ解決する。正本は
`role_provider_binding.py` の `DEFAULT_ROLE_PROFILES`（role→profile）+ `DEFAULT_PROFILE_PROVIDERS`
（profile→provider; `coordination`=codex / `implementation`=claude）。`_DEFAULT_BINDING`（role→
provider）と `DEFAULT_EXPECTED_AGENTS`（launch expected pair）は両方この canonical の **projection**
であり、二重定義しない。expected topology は provider registry から導出しない（known ≠ expected）。

## 3. canonical resolution と current-launch compatibility adapter

- **canonical semantics** = `role → profile → (provider, launch_argv[lane_class])`
  （`AgentsTopologyConfig.resolve_profile_for_role` / `resolve_provider_for_role` /
  `resolve_launch_argv_for_role`）。config semantics の正本はこれ。
- 現行の herdr launch chokepoint は **provider-unit**（pane を provider 単位で起動し、mzb1
  identity の role segment は provider token。起動時に workflow role は存在しない）。よって
  launch は canonical から派生した **current-launch compatibility adapter**
  （`to_resolved_launch_argv_triples` → provider-keyed `AgentLaunchConfig`）を消費する。この
  provider-keyed fold は **canonical ではなく projection** であり、canonical と称さない
  （Redmine #14148 Design Consultation Answer j#84267 条件1）。

### runtime limitation（条件4）

- 同一 provider の複数 named profile を role で区別する構成は **config authority の概念**であり、
  現行の provider-unit launch はまだ反映できない。#14148 は「任意の role 別 profile がすでに
  launch へ反映される」とは主張しない。
- 同一 `(provider, lane_class)` を異なる argv へ束ねる 2 profile は provider-unit launch では
  1 pane に 2 argv を要求する **本質的に launch 不能**な構成であり、**config load 時**
  （最早 side-effect 前境界）に colliding profile 名を明示して fail-closed する。silent な
  select / merge / lane 間継承はしない。
- launch-time の role 別 profile 選択は後続 **Redmine #13647**（launch-time lane-role vocabulary）
  で導入する。そこでは transient launch slot/plan へ `workflow_role + profile_id + provider +
  resolved argv` を whole-plan preflight で固定し、mzb1 durable identity / sender env / target
  authority は provider token のまま維持する。role が一意に解決できない場合は guess せず facade
  または typed fail-closed とする。

## 4. migration（`config migrate`）

公開 `config` group の `config migrate` が v1 → v2 を移行する。`runtime-config`（実体は
`.codex/config.toml` 専用）とは別 surface。

- `--check`（既定）: dry-run。plan と生成される v2 document を表示し **write 0**。
- `--write`: `.bak` backup + temp file + `os.replace` の **atomic write**。書く前に生成 record を
  再検証する。
- **idempotent**: v2 入力は「already v2」で no-op。**lossless**: v1 の effective role→provider と
  provider×lane_class launch argv を v2 の role→profile→launch へ完全保存する（migration 後は
  profile 名でなく resolved role/provider/argv の意味論で比較する）。redundant な既定重複 binding
  （例 `coordinator: codex`）は drop する。
- **registered-adapter 要件**（finding 5）: v1 は open provider を受理するが v2 profile は
  registered adapter id のみ。未登録 provider は silent drop / guess せず
  `registered adapter profile required` + 対象 role/provider（値非機密）で fail-closed。
- provider / default argv を sublane へ merge する fallback は禁止。model 未指定は意図的な値で
  空欄補完対象ではない（#13451 の sublane-has-no-model 不変条件）。

## 5. deprecation / removal lifecycle

- v1 は **`0.13.0` で deprecated**。以後 **最低 2 minor**（`0.13.x` / `0.14.x`）parseable を維持し、
  読み込み時に actionable な deprecation 警告を出す。
- **earliest removal = `0.15.0`**。実際の削除は observable gate（field / dogfood / operator repo に
  v1 config が残っておらず、v1 round-trip fixture を同一 reviewed change で撤去）を満たした後のみ。
  removal は guardrail 変更（独立 issue + review）であり、minor bump の副作用で silent に落とさない。

## 6. trusted executable boundary（item 9）

- repo config から任意実行ファイル / plugin / code を注入できない。`provider` は registered adapter
  id 参照のみ。credential 非接触、shell injection 拒否（既存 launch-argv token validator 再利用）。
- brand 非依存 acceptance（item 8）は provider-swap と、injected/packaged trusted registry 経由の
  第三 fake profile で証明する。repo config から executable/code を注入する acceptance は取らない。

## 7. operator 運用

1. 既存 v1 config は `mozyo-bridge config migrate --check` で v2 plan を確認し、`--write` で移行する。
2. provider を差し替えるときは role を編集せず、profile の `provider` を変える（または role を別
   profile へ束ね直す）。role authority は不変。
3. 同一 provider で coordinator / gateway 等の起動 argv を分けたい場合、現行は lane_class で表現
   できる範囲に限る。role 別の同一-provider profile 分岐は #13647 まで launch へは反映されない。
