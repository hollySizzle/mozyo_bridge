# Bounded Context Map

Redmine Epic/Feature catalog と repo の bounded context / tests / source / docs を対応づける正本。
Feature #12530 `110_テスト構造管理` 配下 US #12488 の成果物。

## 目的

- Redmine の分類 catalog (Epic/Feature) を、repo 側 bounded context 名へ対応づける。
- tests / source / docs の配置判断 (US #12489 配置規約、US #12490 階層移行、Feature #12533 ソース配置) が
  参照する単一の対応表を提供する。
- delegated coordinator / handoff / cockpit のような横断 (cross-cutting) 作業を、特定 Redmine node に
  焼き込まず横断関係として分類できるようにする。

## 非目的

- 本 US では implementation / tests を移動しない。配置判断のみを表で定義する。
- Redmine の日本語表示名 (`110_実行基盤・Routing` など) を directory 名へそのまま焼き込まない。
- Redmine の番号・順序を Python import package 名へそのまま焼き込まない。`110_execution_platform`
  のような数字始まりの component は Python 識別子ではないため、`src/mozyo_bridge/**` と
  `tests/**` の importable package には使わない。
- Redmine catalog node の close / 退役判断は扱わない (別 portfolio 判断)。

## 正本性と命名規約

- Redmine catalog node の正本は Redmine project `giken-3800-mozyo-bridge`。本 doc はその対応表であり、
  Redmine 側の番号・表示名を複製した時点の snapshot を持つ。番号・表示名の正本は常に Redmine。
- repo bounded context 名は **ASCII snake_case** で定義する。Redmine 表示名 (`110_実行基盤・Routing` など)
  を directory / 識別子へ焼かない。表示名と context 名は本表で結ぶ。
- Redmine の Epic / Feature の番号と順序は **mapping metadata** として保持する。repo の
  importable path は番号なしの ASCII snake_case slug を使い、Redmine 番号は catalog table /
  generated reports / review narrative で探索性を担保する。
- Feature-level slug を導入する場合も import-safe にする。例:
  `execution_platform/delegated_coordinator_nested_handoff`。数字始まりの
  `110_execution_platform/140_delegated_coordinator_nested_handoff` は表示・表の値としては使えても、
  Python package path としては使わない。
- 対応は **many-to-many を許す**。1 つの bounded context が複数 Feature を束ね、1 つの作業が複数 context を
  横断しうる (`## Cross-cutting 分類` を読む)。
- runtime package path は `src/mozyo_bridge/`。`vibes/tools/mozyo_bridge` を runtime path として復活させない。

## Redmine order metadata と repo slug

Redmine の Epic / Feature は portfolio ordering を持つため、番号を捨てない。ただし番号は
importable path ではなく metadata として扱う。

| Redmine node | order | repo slug |
|---|---:|---|
| #12501 `110_実行基盤・Routing` | 110 | `execution_platform` |
| #12507 `110_Workspace・Session識別` | 110 | `workspace_session_identity` |
| #12508 `120_AgentDiscovery・Pane解決` | 120 | `agent_discovery_pane_resolution` |
| #12509 `130_HandoffRouting` | 130 | `handoff_routing` |
| #12510 `140_DelegatedCoordinator・NestedHandoff` | 140 | `delegated_coordinator_nested_handoff` |
| #12511 `150_Runtime観測・EventTimeline` | 150 | `runtime_observation_event_timeline` |
| #12512 `160_StateStore・ManagedEvents` | 160 | `state_store_managed_events` |

Source / tests の Feature-level pilot は #12570 が扱う。推奨形は source-first:

```text
src/mozyo_bridge/features/execution_platform/delegated_coordinator_nested_handoff/
tests/unit/execution_platform/delegated_coordinator_nested_handoff/
tests/integration/execution_platform/delegated_coordinator_nested_handoff/
```

Redmine 側の renumber は path rename へ自動反映しない。renumber は mapping metadata の更新として扱い、
repo slug の rename は別 issue で明示的に判断する。

### Full expansion target shape (#12590 / R1 layer-leaf)

US #12590 は #12570 pilot を全 `src/**` へ展開する。target shape は **R1 layer-leaf** (#12591 j#65435):

```text
src/mozyo_bridge/features/<epic_slug>/[<feature_slug>/]<layer>/<module>.py
```

- `layer` ∈ `domain` / `application` / `infrastructure`。`domain/<name>.py` と
  `application/<name>.py` が同名別責務 module として共存する (例 `grandchild_dispatch` /
  `grandchild_stamp` / `delegation_launch_adopt`) ため、flat leaf では衝突する。layer 次元を残す。
- `feature_slug` は execution_platform の定義済み slug のみ (上表 #12507–#12512)。他 Epic は
  Epic-level (`features/<epic_slug>/<layer>/`) に留め、未定義 Feature slug を実装中に新設しない。
- 旧 import path は `sys.modules` facade idiom で温存する (同一 module object 再束縛)。
- 実装は context 別 parallel lane に分割し、#12591 が coordination / integration + 本 shared
  docs/map の唯一の編集 owner (#12590 j#65413 / #12591 j#65454)。詳細方針・固定 surface・residual
  policy は `vibes/docs/logics/source-layout-bounded-context-migration.md` の
  `## #12590 Full Expansion` を正本とする。

| Epic context | 主対応 Epic | child task (move owner) |
|---|---|---|
| `execution_platform` | #12501 | #12592 |
| `operations_cockpit` | #12502 | #12593 |
| `governance_distribution` | #12503 | #12594 |
| `adapter_provider` | #12504 | #12595 |
| `quality_architecture` | #12505 | #12596 |

pilot module `delegation_route_executor` の R1 再配置 (`.../delegated_coordinator_nested_handoff/domain/`)
は #12592 が担当する (#12591 j#65454)。

## Redmine catalog inventory (snapshot)

採番規約: Epic / Feature は独立採番で `110` から 10 刻み。表示名はスペースなし・中点区切り・日本語寄せ。
Feature は機能カタログ node として扱い、作業方針 journal を積まない。

| Epic | Feature |
|---|---|
| #12501 `110_実行基盤・Routing` | #12507 `110_Workspace・Session識別` / #12508 `120_AgentDiscovery・Pane解決` / #12509 `130_HandoffRouting` / #12510 `140_DelegatedCoordinator・NestedHandoff` / #12511 `150_Runtime観測・EventTimeline` / #12512 `160_StateStore・ManagedEvents` |
| #12502 `120_運用Cockpit・表示` | #12513 `110_CockpitReadModel` / #12514 `120_CockpitWebUI` / #12515 `130_Cockpit操作・Preflight` / #12516 `140_表示Grouping・配置` / #12517 `150_Attention・Freshness投影` |
| #12503 `130_統治・Scaffold配布` | #12518 `110_Redmine統治Workflow` / #12519 `120_ScaffoldPreset` / #12520 `130_CanonicalRenderer` / #12521 `140_Rules・DocsCatalog` / #12522 `150_Skill・Plugin配布` / #12523 `160_Release・Version統治` |
| #12504 `140_Adapter・Provider基盤` | #12524 `110_TicketAdapter共通` / #12525 `120_RedmineAdapter` / #12526 `130_AsanaAdapter` / #12527 `140_PresentationProvider` / #12528 `150_PluginManifest・Marketplace` / #12529 `160_ProviderRegistry` |
| #12505 `150_品質・アーキテクチャ統治` | #12530 `110_テスト構造管理` / #12531 `120_シナリオ・受入テスト基盤` / #12532 `130_モジュール健全性管理` / #12533 `140_ソース配置管理` / #12534 `150_CI・検証方針` |
| #12506 `160_外部AgentUI連携` | #12535 `110_VSCodeAgentPane` / #12536 `120_外部LauncherContract` / #12537 `130_AgentPane昇格方針` |

## repo bounded context 定義

repo を 6 つの bounded context へ分ける。各 context は Redmine Epic 1 つに主対応するが、source は DDD layer
(`domain` / `application` / `infrastructure` / `shared`) を横断して同一 context に属しうる。

### `execution_platform`

- 主対応 Epic: #12501 `110_実行基盤・Routing`。
- 責務: workspace / session / pane 識別、agent discovery、handoff routing、delegated coordinator / nested
  handoff、runtime observation / event timeline、state store / managed events。
- 主要 source:
  - workspace/session: `src/mozyo_bridge/workspace_registry.py`、`workspace_defaults.py`、`session_inventory.py`、`domain/session_boundary.py`、`domain/session_naming.py`
  - discovery/pane: `domain/agent_discovery.py`、`domain/pane_resolver.py`
  - handoff/delegation: `domain/handoff.py`、`domain/sublane_callback.py`、`application/cli_handoff.py`、`application/sublane_diagnostics.py`
  - runtime observation: `domain/runtime_observation.py`、`domain/event_timeline.py`、`otel_store.py`、`application/otel_receiver.py`、`application/commands_runtime_observation.py`
  - state: `state_store.py`、`managed_events.py`、`domain/managed_marker.py`、`application/commands_state.py`
  - infra: `infrastructure/queue_reader.py`、`infrastructure/tmux_client.py`
- 主要 tests: `tests/test_handoff_*.py`、`test_agent_*.py`、`test_session_*.py`、`test_workspace_registry.py`、`test_event_timeline.py`、`test_state_store*.py`。
- 注: delegated coordinator の route-plan domain は未実装。classical test 化方針の正本は Redmine #12474
  j#64209 / j#64217、関連 docs は `vibes/docs/logics/coordinator-sublane-development-flow.md`。
  (#12474 の smoke-test-frame doc 化は別 branch にあり origin/main 未統合。merge 後に本 doc から参照を張る。)

### `operations_cockpit`

- 主対応 Epic: #12502 `120_運用Cockpit・表示`。
- 責務: cockpit read model、web UI、操作 / preflight、表示 grouping / 配置、attention / freshness 投影。
- 主要 source:
  - read model: `domain/grouped_read_model.py`、`domain/cockpit_membership.py`、`application/cockpit_page.py`、`application/cockpit_payload.py`、`presentation_state.py`
  - UI / layout: `application/cockpit_ui.py`、`domain/cockpit_layout.py`、`domain/cockpit_geometry.py`、`domain/grouped_display.py`、`domain/grouped_reload_view.py`、`application/grouped_detail.py`、`domain/presentation_grouping/`
  - 操作: `application/cockpit_actions.py`
  - attention: `domain/attention.py`、`application/attention_projection.py`、`application/text_attention_presentation_provider.py`、`application/tmux_attention_presentation_provider.py`
  - cli: `application/cli_cockpit.py`、`application/cli_presentation.py`
- 主要 tests: `tests/test_cockpit_*.py`、`test_grouped_*.py`、`test_attention_*.py`、`test_presentation_*.py`。

### `governance_distribution`

- 主対応 Epic: #12503 `130_統治・Scaffold配布`。
- 責務: Redmine governed workflow、scaffold preset、canonical renderer、rules / docs catalog tooling、
  skill / plugin 配布、release / version 統治。
- 主要 source:
  - scaffold / preset: `src/mozyo_bridge/scaffold/` (packaged preset 正本は `scaffold/presets/**`)
  - docs catalog tooling: `src/mozyo_bridge/docs_tools/` (`catalog.py` / `generate.py` / `impact.py` / `overlay.py` / `resolve.py` / `validate.py`)、`application/cli_docs_scaffold.py`、`application/commands_docs_scaffold.py`
  - release: `application/release.py`、`application/cli_release.py`
- guardrail / 配布 surface (実装 source ではないが本 context に属する): `AGENTS.md`、`CLAUDE.md`、`.mozyo-bridge/rules/**`、`.mozyo-bridge/docs/catalog.yaml`、`skills/**`、`plugins/**`。
- 主要 tests: `tests/test_scaffold.py`、`test_docs_canonical_workspace.py`、`test_bootstrap_install_docs.py`、`test_release_helpers.py`。
- 注: これらの surface 編集権限は中央 preset `### パス別編集権限` と `### Repo-Local Guardrail Autonomous Lane`
  に従う。本 context への対応づけは編集権限を上書きしない。

### `adapter_provider`

- 主対応 Epic: #12504 `140_Adapter・Provider基盤`。
- 責務: ticket adapter 共通契約、Redmine / Asana adapter、presentation provider、plugin manifest /
  marketplace、provider registry。
- 主要 source:
  - ticket adapter: `domain/ticket_adapter.py`
  - Redmine adapter: `infrastructure/redmine_ticket_provider.py`、`infrastructure/redmine_note_transport.py`、`redmine_context.py`、`redmine_credentials.py`
  - presentation provider: `domain/presentation_adapter.py`、`application/presentation_runtime.py`
  - plugin / registry: `domain/plugin_manifest.py`、`domain/provider_registry.py`、`application/provider_runtime.py`
- 主要 tests: `tests/test_provider_registry.py`、`test_provider_runtime_wiring.py`、`test_handoff_delivery_*.py`、`test_text_presentation_provider.py`。
- 注: Asana adapter (#12526) は現状 dedicated provider を持たない seam 段 (`workspace_defaults.py` / `release.py`
  等の参照のみ)。provider US が staged seam で着地する方針は `vibes/docs/logics/plugin-ready-adapter-boundary.md`。

### `quality_architecture`

- 主対応 Epic: #12505 `150_品質・アーキテクチャ統治`。
- 責務: test 構造管理 (本 doc)、scenario / acceptance test 基盤、module 健全性、source 配置、CI / 検証方針。
  他 context のテスト・配置・健全性を統治する **meta context**。
- 主要 source:
  - module health: `domain/module_health.py`、`application/cli_module_health.py`、`application/commands_module_health.py`、repo root `module_health.yaml`
  - module registry: `domain/module_registry.py`、`application/cli_modules.py`
  - test 構造 / source 配置: `tests/` 全体、本 doc
- 主要 tests: `tests/test_module_health.py`、`test_cli_module_registry.py`、`test_mozyo_bridge.py` (構造 assertion)。
- 関連 docs: 本 doc (`bounded-context-map.md`)、`module-health-gate.md`、`refactor-split-strategy.md`、
  `coordinator-sublane-development-flow.md`、および downstream US #12489 / #12490 / Feature #12533。

### `external_agent_ui`

- 主対応 Epic: #12506 `160_外部AgentUI連携`。
- 責務: VS Code Agent Pane、external launcher contract、agent pane 昇格方針。
- 主要 source: `experimental/vscode-agent-pane/` (PoC。`src/mozyo_bridge/` runtime には含めない)。
- 関連 docs: `vibes/docs/specs/vscode-agent-pane-contract.md`、`vibes/docs/specs/vscode-agent-pane-promotion-plan.md`。
- 注: 昇格 (submodule / 独立 repo) 判断は promotion-plan spec に従う。runtime package には焼かない。

## 対応表 (bounded context ↔ Redmine catalog ↔ source ↔ docs)

| bounded context | 主対応 Epic | 主 source root | meta tests | 主 docs |
|---|---|---|---|---|
| `execution_platform` | #12501 | `domain/` + 各 `*.py` (handoff/session/state/observation) | `test_handoff_*` / `test_session_*` / `test_state_store*` | `coordinator-sublane-development-flow.md` / `managed-state-model.md` / `event-timeline-source.md` |
| `operations_cockpit` | #12502 | `domain/cockpit_*` `domain/grouped_*` + `application/cockpit_*` | `test_cockpit_*` / `test_grouped_*` / `test_attention_*` | `cockpit-web-ui.md` / `cockpit-attention-state.md` / `pane-centric-cockpit-semantics.md` |
| `governance_distribution` | #12503 | `scaffold/` + `docs_tools/` + release | `test_scaffold` / `test_docs_*` / `test_release_*` | `scaffold-rules.md` / `canonical-renderer.md` / `release-flow.md` |
| `adapter_provider` | #12504 | `domain/*_adapter.py` `provider_*` + `infrastructure/redmine_*` | `test_provider_*` / `test_handoff_delivery_*` | `plugin-ready-adapter-boundary.md` / `runtime-observability-boundary.md` |
| `quality_architecture` | #12505 | `domain/module_*` + `tests/` 全体 | `test_module_health` / `test_cli_module_registry` / `test_mozyo_bridge` | 本 doc / `module-health-gate.md` / `refactor-split-strategy.md` |
| `external_agent_ui` | #12506 | `experimental/vscode-agent-pane/` | (PoC; 最小) | `vscode-agent-pane-contract.md` / `vscode-agent-pane-promotion-plan.md` |

source の DDD layer (`domain` / `application` / `infrastructure` / `shared`) は context 横断の技術階層であり、
context 境界とは直交する。`shared/` (`errors.py` / `name_compat.py` / `paths.py`) は全 context 共有基盤。

## Cross-cutting 分類

一部の作業は単一 Redmine node に収まらず複数 bounded context を横断する。これらは Redmine 階層へ焼き込まず、
**横断関係**として本 doc に記録する。代表例:

### delegated coordinator / nested handoff (#12473 / #12474)

親子孫 delegated coordinator の visible sublane 実現と 3 階層 cockpit 表示 (#12454 配下 #12473 実装 /
#12474 smoke) は、次の context を同時に触る:

- `execution_platform`: handoff routing (#12509)、delegated coordinator / nested handoff (#12510)、
  route-plan / dispatch decision (未実装 domain、#12474 j#64217 の classical test 化対象)。
- `operations_cockpit`: cockpit read model (#12513) と attention / freshness 投影 (#12517)。
  `agents targets` の `KIND` / `DEPTH` / `PARENT` 列は route-bound metadata の投影であり、表示と route は
  分離して評価する (#12474 受入条件)。
- `adapter_provider`: Redmine read-boundary 分類 (allowed / insufficient / contaminated)。
  sparse anchor からの委任推論で、parent journals / sibling 越境読みを contamination 扱いする (#12474 j#64172 / j#64185)。
- `quality_architecture`: 実機 E2E smoke を主検証にせず classical test harness へ落とす方針 (正本 Redmine
  #12474 j#64209 / j#64217)。

この作業を「handoff の話だから #12509 だけ」のように単一 node へ焼き込むと、cockpit 表示 / read-boundary /
test 層の関係が失われる。よって delegated coordinator は **cross-cutting concern** として本節で束ね、各 context の
Feature へは「横断対応」として紐づける。

### その他の横断パターン

- handoff delivery record / sink: `execution_platform` (handoff) と `adapter_provider` (delivery sink seam) を
  横断する (`plugin-ready-adapter-boundary.md`)。
- attention / freshness: `operations_cockpit` (投影) と `execution_platform` (runtime observation source) を横断する。
- module health gate: `quality_architecture` (gate 定義) が全 context の source module を計測対象に取る。

## Downstream 利用

本 doc は配置判断の上流であり、次の US / Feature が参照する:

- US #12489 `tests 配置規約と discovery 方針`: 本表の context ↔ tests 対応を入力に、tests directory 規約と
  discovery 方針を定義する。
- US #12490 `tests フラット構造を bounded context / test type 階層へ移行`: 本表を移行先 mapping として使う。
- Feature #12533 `140_ソース配置管理`: source layout の bounded context 移行計画が本 context 定義を参照する。
- Feature #12532 `130_モジュール健全性管理`: module health gate (`module-health-gate.md`) の計測対象を context
  単位で読み替える際に参照する。

本表の Redmine 番号・表示名は snapshot である。Redmine 側 catalog が更新されたら本 doc を同じ作業単位で追従する。
