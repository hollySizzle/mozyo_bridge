# Scaffold package boundary 最終方針 (governance_distribution 帰属 + justified residual)

Redmine #12641 (parent Feature #12533 `140_ソース配置管理`、Version #276 `OOP-first
architecture and static typing`)。`src/mozyo_bridge/scaffold/**` の runtime /
package-resource / preset-resource 責務を棚卸しし、bounded context 上の所有境界と
physical layout の最終 disposition を確定する **boundary 決定正本**。

> 本 doc は boundary 決定 + 配置正当化 + 責務分割方針を定義する **方針 doc** である。
> 本 US では source / resource を物理移動しない (behavior-preserving residual)。実際の
> 移動・packaging glob 変更・resource anchor 変更は末尾 `## Move-enabling conditions`
> を満たす別 issue + Design Consultation で行う。位置づけは #12640
> [[logic-shared-kernel-freeze]] (`shared/**` の freeze 決定) と同型 — top-level technical
> package の最終 disposition を per-concern で確定し、強制移動はしない。

## 背景

- `src/mozyo_bridge/scaffold/**` は #12631 / #12632 の Redmine-numbered layout correction
  完了後も、top-level 横並びに残る **最後の technical package** である
  ([[logic-source-layout-bounded-context-migration]] の Fixed / package-data residual 表)。
- bounded context map 上、scaffold は `governance_distribution` (Epic #12503 =
  `e_130_governance_distribution`) の Feature #12519 `120_ScaffoldPreset` =
  `f_120_scaffold_preset` に帰属する ([[spec-bounded-context-map]])。すなわち所有境界は
  既に確定しており、未確定なのは **physical path を numbered layout へ移すか否か** のみ。
- `scaffold/rules.py` は 1081 行で module-health gate を僅かに超過する residual
  (baseline owner_issue #12321、[[logic-module-health-gate]])。`scaffold/canonical.py` は
  370 行で gate 下。
- #12590 / #12591 / #12622 / #12623 の全 migration round が scaffold を一貫して
  **held-fixed (package-data 結合)** として据え置いてきた。本 US はその据え置きを
  「未決」から「正当化された確定 residual」へ昇格させる。

## 決定 (Decision)

**Justified residual。** `src/mozyo_bridge/scaffold/**` を top-level package として残置する。

- **論理所有 (logical ownership)** は `e_130_governance_distribution / f_120_scaffold_preset`
  に pin する。bounded context map 上の帰属は Epic-level ではなく Feature-level で確定する。
- **physical path** は `src/mozyo_bridge/scaffold/**` のまま動かさない。これは numbered
  layout (`e_<order>/f_<order>/<layer>/<module>.py`) に対する **明示的 packaging 例外** として
  記録する。`e_<order>/f_<order>/` ディレクトリへの物理移動は本 US では行わない。
- import path `mozyo_bridge.scaffold.*` は public seam として固定する。

この決定は behavior を一切変えない。`scaffold canonical --check` / `scaffold status` は
本 US で drift を出さない (no-op)。

## Residual の正当化 (coupling inventory)

scaffold を numbered path へ移すには、behavior-preserving (move-only) であっても次の
複数 surface を **同一移動で同時変更** する必要があり、packaging / resource-resolution /
build の失敗面を新たに開く。behavior 利得はゼロのため、本 US では move を正当化できない。

1. **pyproject `[tool.setuptools.package-data]` glob** — `scaffold/presets/**` /
   `scaffold/canonical_sources/**` を resource として同梱する glob 群。directory rename は
   build 出力 (wheel) に直結し、dotfile (`.gitignore` / `.claude-nagger/**`) を含む glob は
   既に finicky (pyproject 内 comment 参照)。rename = build 破壊リスク。
2. **`importlib.resources` anchor ×3** — `rules.py` が
   `resources.files("mozyo_bridge.scaffold.presets")` を 3 箇所 (registry / preset root /
   router template) で hardcode する。move は wheel からの resource 解決を含む anchor の
   一括変更を要する。
3. **`canonical.py` の repo-relative 定数 ×2** — `CANONICAL_SOURCES_RELATIVE =
   src/mozyo_bridge/scaffold/canonical_sources` / `SCAFFOLD_OUTPUT_ROOT_RELATIVE =
   src/mozyo_bridge/scaffold`。canonical render + drift gate がこの path に依存する。
4. **配布正本境界 docs の path 焼き込み** — [[logic-scaffold-distribution-minimization]] の
   `正本境界` (`package.path: src/mozyo_bridge/scaffold/**`)、[[logic-scaffold-rules]]、
   [[logic-canonical-renderer]] が distribution source を `src/mozyo_bridge/scaffold/**` と
   明記する。move は複数 doc の一括追従を要する。
5. **module-health baseline + downstream importers** — `module_health.yaml` の
   `scaffold/rules.py` baseline、6 件の source importer
   (`e_130_governance_distribution/f_140_rules_docs_catalog/application/{cli,commands}_docs_scaffold.py`、
   `e_130_governance_distribution/f_160_release_version_governance/application/release.py`、
   `application/doctor.py`、`application/doctor_scaffold.py`、`application/commands.py`)、
   および scaffold 系 tests。

→ これは #12590 / #12622 / #12631 が scaffold を held-fixed としてきた理由と同一構造であり、
j#69241 が「package-resource migration risk is too high なら residual を rationale 付きで
記録する」と pre-authorize した状況に該当する。よって move を強制せず residual を確定する。

## rules.py / canonical.py 責務分割方針

- **`canonical.py` (370 行)** — pure conditional renderer。threshold 下で責務も単一
  (canonical source → fragment 結合 → drift 検出)。**分割不要、現状維持。**
- **`rules.py` (1081 行)** — over-gate residual (#12321 baseline)。責務クラスタは既に明瞭:
  1. preset registry / definition 読み込み (`_load_preset_registry` / `preset_definition`)。
  2. rules store 解決 (`RulesStore` / central・repo-local mode)。
  3. router 描画 + project-local block 保存 (`render_router_pair` /
     `apply_project_local_preservation`)。
  4. preset extra-files の category opt-in/opt-out 描画 (`render_preset_extra_files`)。
  5. manifest + `scaffold status` drift 判定 (`manifest_content` / `scaffold_status`)。
- 将来 `rules.py` を分割する場合は [[logic-refactor-split-strategy]] の behavior-preserving
  file-split に従い、上記 family を module へ外出しする。ただし **resource anchor
  (`resources.files("mozyo_bridge.scaffold.presets")`) と package directory を動かさない**
  純 file-split に限る (packaging risk なし)。本 US では split を実行せず、`rules.py` split も
  resource-move とは独立した residual として別 issue に委ねる。

## Move-enabling conditions (residual 見直し条件)

次のいずれかが成立したとき、本 residual を再評価し physical move を検討する:

- numbered-layout migration が package-data 結合 resource tree を packaging-safe に移す
  機構 (glob 自動追従 / resource anchor の path-prefix 非依存化) を確立したとき。
- `importlib.resources` anchor を package path に非依存化する helper が入り、`canonical.py`
  の repo-relative 定数も derive 可能になったとき。
- 配布正本境界 docs の `src/mozyo_bridge/scaffold/**` 参照を一括追従する owner issue が
  立ち、move を single coordinated unit として扱えるとき。

これらが揃うまでは、scaffold は `f_120_scaffold_preset` 論理所有 + top-level physical
residual の確定状態を維持する。

## 非目的

- `application/commands.py` の OOP 分解 (別 US)。
- `shared/**` の整理 (#12640 [[logic-shared-kernel-freeze]] で別途確定済み)。
- release / version bump / publish action。
- `scaffold/presets/**` の package-data glob を壊す rename。
- 配布物 shape / router semantics / registry schema の変更
  ([[logic-scaffold-distribution-minimization]] の escalation triggers に従う)。

## 検証

- `mozyo-bridge docs validate --repo .` ほか catalog 検証一式 (本 doc の catalog 登録)。
- `mozyo-bridge docs validate --repo . --check-file-coverage` (`vibes/docs` coverage root に
  本 doc を含めるため)。
- `mozyo-bridge docs generate-file-conventions --repo . --check` (catalog 変更後の generated
  整合)。
- `mozyo-bridge docs audit-impact --all-changed --check-generated`。
- `mozyo-bridge docs resolve vibes/docs/logics/scaffold-boundary.md --repo . --format text` で
  関連 docs の双方向解決を確認。
- source / resource を動かさないため `scaffold canonical --check` / `scaffold status --target .`
  は drift を出さない (no-op 確認)。
- `git diff --check`。
