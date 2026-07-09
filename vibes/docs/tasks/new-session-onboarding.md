# 新規セッション onboarding 導線 (fresh agent 向け)

新規 session の agent (coordinator / worker いずれも) が chat context を持たずに立ち上がっても、context を失わず現在地に到達するための **安定した導線** (Redmine #13406)。本書は **state snapshot を焼かない** — 「今どこか / 次に何をするか」の live な正本は Redmine journal であり、本書はその読む順序と在り処だけを固定する pointer である。

## 読む順序 (最初の 5 分)

1. **`CLAUDE.md` (Codex は `AGENTS.md`)** — tool-specific router。session 開始手順と常時適用規則ダイジェストを読む。
2. **central preset** `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md` — gate / 役割 / 編集権限 / completion の governance 契約の正本。role/provider binding の運用値は `.mozyo-bridge/config.yaml` の `provider_binding` を確認する (自役割を自認する前に)。
3. **現在の作業の Redmine issue / journal** — durable record が作業状態の正本。着手前に active issue を読む。どの issue が active かは §「現在地の見つけ方」参照。
4. **質問ドメインの cataloged docs** — 設計・仕様・現状挙動を回答/断定する前に、`mozyo-bridge docs resolve <path>` で正本を解決して読む (always 規則 `answer-time-doc-resolution`)。

## Redmine 構造 (機能カタログ)

Epic / Feature は「プロジェクトが成長しても使える機能カタログ」として設計されている (release grouping ではない)。現在地は **Feature 単位** で探す。

- `110_実行基盤・Routing` (#12501) — workspace/session identity, agent discovery, handoff routing, delegated coordinator, runtime 観測, state store。**herdr backend swap = Feature `170_herdr backend swap` (#13242)** ← 直近の主戦場。
- `120_運用Cockpit・表示` (#12502) — cockpit read model / web UI / preflight / grouping / attention。
- `130_統治・Scaffold配布` (#12503) — Redmine 統治 workflow, scaffold preset, canonical renderer, **`140_Rules・DocsCatalog` (#12521)** ← 本 onboarding doc / 規約系の親。
- `140_Adapter・Provider基盤` (#12504) — ticket adapter, redmine/asana adapter, presentation provider, plugin/marketplace, provider registry。
- `150_品質・アーキテクチャ統治` (#12505) — テスト構造, **`120_シナリオ・受入テスト基盤` (#12531)** ← 古典 scenario テスト基盤の親, モジュール健全性, source 配置, CI。
- `160_外部AgentUI連携` (#12506) — VS Code Agent Pane 系 (遠期 backlog)。

新規 US は該当 Feature 配下に置く。Version は release grouping ではなく実行 bucket / conflict bucket (`feedback_coordinator_fill_ritual_and_redmine_scope` / `vibes/docs/logics/release-flow.md`)。

## 現在地の見つけ方 (live state)

1. `mozyo-bridge docs resolve` は catalog 経由で「触る path に紐づく正本」を返す。
2. active US は Redmine で `list_user_stories(status=open)` / `list_recently_updated_issues`。現行 program の open backlog は主に Feature #13242 (herdr) と #12531 (test 基盤) 配下。
3. `mozyo-bridge status` / `herdr agent list` / `herdr workspace list` は runtime の観測 (durable record ではない — 判断の正本は Redmine)。

## 主要 doc の在り処 (vibes/docs レイアウト)

| dir | 用途 | 主な doc |
| --- | --- | --- |
| `logics/` | 業務ロジック | `coordinator-sublane-development-flow.md` / `delegated-coordinator-cockpit-display.md` / `sublane-lifecycle-map.md` / `herdr-scenario-test-foundation.md` |
| `rules/` | プロジェクト規約 | `agent-workflow.md` / `workflow-docs-boundary.md` / `public-private-boundary.md` / `codex-autonomous-guardrail-lane.md` |
| `specs/` | 仕様 | `herdr-native-identity.md` / `route-identity-ledger.md` / `delegation-policy-project-config.md` |
| `tasks/` | 手順書 | `herdr-lane-operations.md` (lane 運用) / `external-project-herdr-adoption.md` (他 project 導入) / 本書 |
| `temps/` | 一時ドキュメント | 恒久参照にしない。stale は掃除対象。 |

## herdr 運用の要点 (2026-07-09 時点の確立事項)

以下は spec/task doc に正本があり、ここでは pointer のみ:

- lane は **project workspace とは別の専用 sublane host workspace** に着地する (coordinator window + sublane window の分離、正本 `logic-delegated-coordinator-cockpit-display` / #13380)。
- lane identity = `mzb1_<project-ws>_<role>_<lane>`、新 lane 標準形 = 単発 `sublane create --execute`、explicit lane 送達 = `--target-lane` (正本 `spec-herdr-native-identity` / `task-herdr-lane-operations`)。
- lane 内 nested handoff send は outer repo root を `--repo` で pin する (外部 project で backend 乖離を防ぐ、#13397)。
- 未採用 directory の bare `mozyo` は fail-closed (採用 = scaffold/config marker、Git 非条件、home 常時拒否、正本 `task-external-project-herdr-adoption`)。
- CLI 版ズレ時 (installed CLI が最新 main 未反映) の lane 運用回避は `task-herdr-lane-operations` の前提節を読む。

## 禁止・注意

- agent-private memory は補助。**harness の正本は本 repo の vibes/docs + Redmine + central preset**。memory の「解消済み」記録は scope を疑い、正本で照合する (`answer-time-doc-resolution`)。
- 記録に host-local 絶対 path / secret を書かない (`rule-public-private-boundary`)。
