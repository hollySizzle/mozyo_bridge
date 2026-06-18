# Scaffold 配布物の最小化と workspace anchor 中心モデル

Redmine #11428。各 workspace へ `.mozyo-bridge` 周辺ファイルを増殖させる配布モデルを縮小し、home registry と薄い workspace anchor を中心にした構成へ移行するための **方針正本 (classification + minimization policy + migration/compat boundary)**。

> 本 doc は配布物の分類と最小化の目標像・移行/互換境界を定義する **方針 doc** である。実際の artifact 削除・配布 shape 変更・router semantics 変更は伴わない。それらは各々の escalation 条件 (末尾) に従い、個別 issue + Design Consultation で行う。

## 背景と前提

`#11420` 系で identity 基盤が揃った:

- `#11425`: home-registry-first identity (`registry.sqlite` 正本 + workspace-local anchor `.mozyo-bridge/workspace.json`) 実装済み。設計正本は [[logic-workspace-registry]]。
- `#11426`: `doctor` が registry / anchor / runtime の整合性診断へ拡張済み。
- `#11427`: smart `init` が registry-aware (未登録 workspace を登録、既登録は canonical session 再利用、role bind を pane option で安定化)。

したがって `#11428` は runtime identity の追加実装ではなく、**scaffold 配布物・governance docs・移行/互換境界の整理** が中心である。scaffold 配布の設計正本は [[logic-scaffold-rules]]、bootstrap は [[logic-bootstrap]] を読む。

## 正本境界 (再確認)

```yaml
正本境界:
  package:
    role: 配布元 (distribution source)。preset 本文・router canonical source・governed artifact 雛形・catalog skeleton は CLI package が持つ
    path: src/mozyo_bridge/scaffold/**
  home_registry:
    role: workspace identity の正本 (workspace_id / canonical path / canonical session / preset version)
    path: "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/registry.sqlite"
  central_rules_store:
    role: 実行時 guardrail (preset agent-workflow.md) の既定配置
    path: "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/"
  workspace_local:
    role: 復元 anchor + tool entrypoint + (opt-in) governance metadata。必要最小限に保つ
    path: <repo>/AGENTS.md, <repo>/CLAUDE.md, <repo>/.mozyo-bridge/**
```

設計上の不変条件:

- **home registry を唯一の復元路にしない。** workspace-local anchor (`.mozyo-bridge/workspace.json`) は portable な復元 primitive として残す ([[logic-workspace-registry]] の anchor 不変条件)。home 喪失時の identity 再構築は anchor / derivation が担う。
- **thin router を太らせない。** `AGENTS.md` / `CLAUDE.md` は central preset を指す router であり、重い guardrail 本文を inline しない ([[logic-scaffold-rules]] の Router Template Single Source / Drift Detection)。
- **private path / operator-specific policy を OSS default に混ぜない。** 公開 template / packaged preset / central public preset に host path・credential・個人 policy を入れない (public/private boundary は [[rule-public-private-boundary]])。
- **registry schema / `#11425` identity semantics を変更しない。** 本 doc の最小化は配布物の置き場所と必要性の整理であり、identity model の変更ではない。

## 配布物の分類

workspace に現れる mozyo-bridge artifact を 4 カテゴリに分類する。出所 (scaffold 配布 / register 書き込み / 生成 / operator 作成) と「今後の置き場所方針」を併記する。

```yaml
categories:
  A_durable_identity_restoration_anchor:
    定義: workspace identity を portable に復元するための最小 record。home 喪失時の種
    workspace_local必須: true
  B_thin_tool_entrypoint_router:
    定義: agent runtime (Claude / Codex) が repo root で読む薄い router。central preset を指すのみ
    workspace_local必須: true (現行 UX)
  C_optional_governance_docs_catalog:
    定義: governed preset が opt-in で配る governance / docs catalog 素材
    workspace_local必須: false (opt-in。catalog 運用を維持する project のみ)
  D_generated_or_bootstrap_only:
    定義: 生成物、または install 記録。正本ではなく再生成・再導出可能
    workspace_local必須: false (正本ではない。drift 検出 / 再生成で扱う)
```

### artifact 判定表

| artifact | 出所 | category | 置き場所方針 |
|---|---|---|---|
| `AGENTS.md` / `CLAUDE.md` | scaffold (`scaffold apply`) | B | workspace-local 必須 (thin router)。最小化対象だが削除しない。本文を太らせず central preset 参照を維持 |
| `.mozyo-bridge/scaffold.json` | scaffold | D | install manifest (drift 検出用)。正本ではない。`scaffold status` が消費。workspace-local だが regenerable |
| `.mozyo-bridge/workspace.json` (anchor) | `workspace register` | A | workspace-local 必須。home 喪失復元の portable 種。path を持たない (置き場所自体が path) |
| `.mozyo-bridge/workspace-defaults.yaml` | operator 作成 | C (operator-owned) | optional。Redmine default project 等の workspace-local 設定 source。配布物ではない ([[logic-workspace-defaults-renderer]]) |
| `.mozyo-bridge/rules/presets/<preset>/agent-workflow.md` | `rules install --repo-local` | C (Dev Container mode のみ) | central store が既定。repo-local mode は ephemeral-home 向け opt-in。第三の配布 mode を増やさない |
| `.mozyo-bridge/rules/llm_rule_authoring.md` | scaffold (governed) | C | governed opt-in。central / package 寄せの再評価候補 (下記) |
| `.mozyo-bridge/rules/docs_catalog_governance.yaml` | scaffold (governed) | C | governed opt-in。catalog 運用 project のみ必要 |
| `.mozyo-bridge/docs/catalog.yaml` | operator (`.example` から複製) | C (operator-owned) | active-doc map。operator 正本。scaffold は `.example` のみ配り本体に触れない |
| `.mozyo-bridge/docs/catalog.yaml.example` | scaffold (governed) | C / skeleton | governed opt-in の skeleton。framework-neutral を維持 |
| `.mozyo-bridge/docs/.gitignore` | scaffold (governed) | C | governed opt-in。local-only overlay (`catalog.local.yaml`) を git-ignore する catalog skeleton 付随ファイル |
| `.mozyo-bridge/docs/catalog.local.yaml` (overlay) | operator (local-only) | C (operator-owned) | optional。checkout-local docs を catalog 解決へ足す git-ignored overlay。配布物ではなく operator が必要な checkout だけに置く ([[rule-public-private-boundary]]) |
| `.mozyo-bridge/docs/file_conventions.generated.yaml` | `docs generate-file-conventions` | D | 生成物。正本にしない。catalog から再生成 |
| `.mozyo-bridge/tmux/agent-ui.conf` | scaffold (governed) | C | governed opt-in の tmux UI snippet。host wiring は別途 opt-in |
| `.claude-nagger/{config,command_conventions,mcp_conventions}.yaml.example` | scaffold (governed) | C | governed opt-in の nagger skeleton。`config.yaml` 本体は operator が複製するまで無効 |
| `.claude-nagger/.gitignore` | scaffold (governed) | C | nagger skeleton 付随 |

## 配布責務の再評価 (central rules / preset / catalog skeleton)

現行の責務配置を確認し、最小化の方向性を示す (実装変更は伴わない)。

- **central rules (preset `agent-workflow.md`)**: 既定は home central store。重い guardrail はここに集約され、router は薄いまま。これは現行で既に最小化方針に沿う。repo-local mode は Dev Container / ephemeral-home の例外配布で、`#11428` で増やさない。
- **preset 配布 (package → home/repo-local)**: package が唯一の配布元。`rules install` が home へ、`--repo-local` が repo へ copy。配布 mode は central / repo-local の 2 つに固定 ([[logic-scaffold-rules]] Central Rules Management)。**第三の mode (vendor / symlink / release-pin export) を default に増やさない。**
- **catalog skeleton (`catalog.yaml.example`)**: governed opt-in。本体 `catalog.yaml` は operator 正本で scaffold は触れない。framework-neutral skeleton を維持。
- **governed artifacts (C カテゴリ)**: 現状すべて workspace-local に配られる。最小化の検討余地:
  - `llm_rule_authoring.md` / `docs_catalog_governance.yaml` は内容が project 非依存であれば central preset 同梱 (home 側) へ寄せ、workspace-local copy を減らせる可能性がある。ただし catalog governance tooling が repo-local catalog を相手にする以上、repo-local 参照の要否を含めた評価が要る。
  - 実際に workspace-local copy を廃止/縮小する変更は **配布 shape 変更**であり、既存 adopter 互換に影響するため Design Consultation を要する (末尾 escalation)。本 doc では「寄せられる候補」として分類するに留める。

## 最小化の目標像 (target model)

workspace に **必須** なのは次の最小集合:

```yaml
workspace_minimal_set:
  - AGENTS.md            # B thin router (tool entrypoint)
  - CLAUDE.md            # B thin router (tool entrypoint)
  - .mozyo-bridge/scaffold.json   # D install manifest (drift 検出)
  - .mozyo-bridge/workspace.json  # A 復元 anchor (register が書く)
optional_opt_in:
  - .mozyo-bridge/rules/**        # governed / repo-local mode
  - .mozyo-bridge/docs/catalog.yaml(.example) + file_conventions.generated.yaml
  - .mozyo-bridge/tmux/agent-ui.conf
  - .claude-nagger/**
  - .mozyo-bridge/workspace-defaults.yaml  # operator-owned
```

方針:

- **A / B は workspace-local に残す。** identity 復元 anchor と tool entrypoint は portable に repo へ置く必要がある。
- **C は opt-in を明確化し、不要 project には配らない。** governed preset を選んだ project だけが C を持つ。lightweight preset (`redmine` / `none` 等) は router + manifest のみ。
- **D は正本にせず、生成 / drift 検出で扱う。** `scaffold.json` は manifest、`file_conventions.generated.yaml` は生成物。
- governed C 群のうち project 非依存なものを central/package へ寄せる検討は将来の個別 issue。**本 doc では候補分類まで。**

## 移行手順と後方互換

- **後方互換 (既存 workspace)**: 本 doc は配布物を削除しないため、既存 workspace の artifact はそのまま有効。`scaffold.json` schema は v2 を既読で、pre-v2 は `scaffold status` が fail-and-upgrade を出す ([[logic-scaffold-rules]] CLI Shape)。registry / anchor の後方互換は [[logic-workspace-registry]] の通り (anchor restore / derivation fallback)。
- **段階的移行**: 最小化は「新規 default を絞る」「既存 C を opt-out 可能にする」方向の additive 変更として段階導入する。一括削除や強制移行はしない。
- **可視化**: 移行状態は `scaffold status` (manifest drift) と `doctor` の `scaffold` / `workspace_registry` section ([[logic-workspace-registry]] / `#11426`) で可視化する。operator はこれらを見て C を保持/除去を判断する。
- **anchor 中心への寄せ**: workspace identity の再取得は anchor → registry restore (`workspace register`) / smart `init` (`#11427`) で行えるため、identity 面では `.mozyo-bridge` の他 metadata に依存しない。

## private / operator policy 分離

- 公開 template / packaged preset / central public preset に host path・credential・個人 policy・private URL を入れない ([[rule-public-private-boundary]] と [[logic-scaffold-rules]] Common constraints)。
- repo-local mode の `rule_path` は repo-relative を維持し、host 絶対 path を committed router / manifest に漏らさない ([[logic-scaffold-rules]] Repo-Local Rules Mode)。
- operator-specific cockpit composition / lane policy は OSS default に混ぜない ([[logic-coordinator-sublane-development-flow]])。

## scope 境界 / Design Consultation triggers

次のいずれかを要する変更は本 issue に含めず、個別 issue + Design Consultation で行う:

- 現に配布済みの artifact を migration / backcompat story なしで削除する。
- `AGENTS.md` / `CLAUDE.md` の生成 router semantics を変更する、または router を廃止/意味変更する。
- registry schema または `#11425` identity semantics を変更する。
- scaffold preset の配布 surface を既存 adopter 互換に影響する形で変更する (C 群の central 寄せ等)。
- `#11887` の `catalog.local.yaml` overlay 配布を本 scope に混ぜる。

## 検証

- `mozyo-bridge docs validate --repo .` ほか catalog 検証一式 (本 doc の catalog 登録時)。
- `mozyo-bridge docs validate --check-file-coverage --repo .` (logics coverage root に本 doc を含めるため)。
- `mozyo-bridge docs generate-file-conventions --repo . --check` (catalog 変更後の generated 整合)。
- `mozyo-bridge docs resolve vibes/docs/logics/scaffold-distribution-minimization.md --repo . --format text` で関連 docs 解決を確認。
- 配布物 shape は変更しないため `scaffold canonical --check` / `scaffold status` は本 doc では drift を出さない (no-op 確認)。
