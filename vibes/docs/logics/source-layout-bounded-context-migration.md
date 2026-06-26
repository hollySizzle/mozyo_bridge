# Source Layout Bounded-Context Migration Plan

Redmine #12492 (parent #12533 `140_ソース配置管理`)。`src/mozyo_bridge` の現行
`application/` / `domain/` 横並び (technical-layer) 構造を棚卸しし、Redmine Feature
対応の bounded context へ段階的に寄せるための移行計画を定義する設計正本。

この文書は docs-only の計画であり、実装差分 (source move) を含まない。実際の移動は
本計画が定義する低リスク単位を per-issue で実装する (最初の単位は #12493、本計画の
follow-up として切り出す)。一括移動は禁止する。

> 後続補正 (#12570): source/test ownership layout は Redmine Epic/Feature の意味と順序を
> 踏襲するが、Python import package path には数字始まりの `110_...` component を使わない。
> Redmine 番号は `bounded-context-map.md` の mapping metadata として保持し、source path は
> import-safe な ASCII snake_case slug を使う。旧 `contexts/<name>/` 候補は履歴上の計画名であり、
> #12570 の pilot では `features/<epic_slug>/<feature_slug>/` 形を優先して検証する。

関連する既存正本:

- `vibes/docs/logics/refactor-split-strategy.md` (#12002): 巨大 file を feature family
  単位で behavior-preserving に分割する方針。本計画はその上位 (package 境界) を扱い、
  file split 方針と矛盾しない。
- `vibes/docs/logics/plugin-ready-adapter-boundary.md` (#12001): built-in adapter 境界
  (ticket / presentation / runtime / catalog / telemetry / release helper) の分類。
  bounded context の adapter seam はこの分類に従う。
- `vibes/docs/logics/modular-config-driven-refactor.md`: config-driven module 分割方針。
- `vibes/docs/logics/managed-state-model.md` / `runtime-observability-boundary.md`:
  managed state / observability の正本境界。

## Current Measurement

2026-06-24 時点の実測 (`src/mozyo_bridge`、`__pycache__` 除外):

```text
application/      files=46  lines=19640
domain/           files=36  lines=15428
infrastructure/   files=5   lines=647
scaffold/         files=4   lines=1401   (+ canonical_sources / presets ツリー)
docs_tools/       files=7   lines=1243
shared/           files=4   lines=200
top-level loose   *.py = 11 modules
```

top-level の loose module:

```text
__init__.py  __main__.py
managed_events.py  otel_store.py  presentation_state.py
redmine_context.py  redmine_credentials.py  session_inventory.py
state_store.py  workspace_defaults.py  workspace_registry.py
```

現行構造は **technical layer** (application / domain / infrastructure / shared) で
切られており、Redmine Feature (handoff / coordinator / cockpit / governance / scaffold /
release / plugin adapter) の所有境界と直交している。1 つの Feature が application と
domain と top-level loose module に分散しているため、所有者と影響範囲が読みにくい。

## Current Layering Observations

移行計画の前提として、現状の依存方向を実測した。

- `infrastructure → application` の依存は **無い** (grep で当たる `application` は
  `"Content-Type": "application/json"` の文字列のみ。import 違反ではない)。
- `domain → infrastructure` の実 import が 3 module に存在する。いずれも
  `infrastructure.tmux_client` への依存:
  - `domain/agent_discovery.py` → `tmux_client.pane_lines`
  - `domain/managed_marker.py` → `tmux_client.set_user_option` / `get_user_option`
  - `domain/pane_resolver.py` → `tmux_client` (複数)
  - (`domain/delivery_record_sink.py` / `domain/ticket_adapter.py` は docstring で
    infrastructure を言及するのみで、実 import は持たない。)
- `application → infrastructure` は期待通りの方向 (3 file 程度)。

帰結: 現行の `domain/` は「pure domain」ではなく、一部に tmux IO へ降りる adapter が
混在している。bounded context 移行と同時に layer purity を全面修正しようとすると
risk が跳ね上がるため、本計画は **context 再配置を主目的とし、layer purity 修正は
context 内の follow-up に分離する** 方針を採る (Non-Goals 参照)。

## Target Bounded Contexts

最終形は Redmine Feature 対応の bounded context に寄せる。各 context は「core (pure
decision / records)」と「adapter (IO / provider)」を内側に持ち、context 間は
shared kernel と公開済み record 型のみで結合する。最終的な Feature 名との対応は
Redmine Feature ツリー (#12533 配下) で確定するため、本計画では候補マッピングを示す。

### 1. shared kernel (foundational)

- 現行: `shared/errors.py` `shared/paths.py` `shared/name_compat.py`
- 役割: 全 context が依存してよい最下層。例外型・path 解決・後方互換名のみ。
- 移行: 既に独立しているため移動しない。kernel が他 context を import しない不変条件を
  維持する (循環防止の要)。

### 2. managed-state / runtime (foundational)

- 候補配置先: `core/state/`
- 現行モジュール: `state_store.py` `managed_events.py` `session_inventory.py`
  `otel_store.py` `workspace_registry.py` `workspace_defaults.py`、`domain/event_timeline.py`
  `domain/runtime_observation.py`、`presentation_state.py`
- 役割: managed desired-state event log / registry / projection / OTel store の正本
  (`managed-state-model.md` / `runtime-observability-boundary.md`)。
- 移行: top-level loose module を 1 つずつ package へ寄せる。import path 影響が大きい
  (後述) ため facade 経由で段階移行する。

### 3. handoff context

- 候補配置先: `contexts/handoff/`
- 現行モジュール: `domain/handoff.py` `domain/delivery_record_sink.py`
  `domain/sublane_callback.py` `domain/notification.py`、`application/cli_handoff.py`、
  `application/commands.py` の handoff/notify family、`infrastructure/queue_reader.py`
  `infrastructure/redmine_note_transport.py` の handoff 配送部。
- 役割: durable anchor 正規化、delivery outcome 構築、sublane callback、notify 互換。
- adapter seam: ticket-WRITE / note transport は `plugin-ready-adapter-boundary.md` の
  ticket adapter に従い、core 所有の fail-closed seam + injected transport を保つ。

### 4. delegated-coordinator context

- 候補配置先: `contexts/coordinator/`
- 現行モジュール: `domain/sublane_callback.py` (handoff と共有)、`application/sublane_diagnostics.py`、
  coordinator/sublane dispatch flow に関わる command family
  (`coordinator-sublane-development-flow.md` / `sublane-bandwidth-policy.md`)。
- 注記: `project_router.py` / project-routing command は **本 base (origin/main
  `84b475c`) には存在しない** (別 lane tip のみ)。本計画は現 base に存在する module の
  みを対象とし、project router が main へ統合された時点で本 context に編入する。
- 移行: handoff context と境界を共有するため、両者の record 型を先に切り出してから
  module を分ける。

### 5. cockpit context

- 候補配置先: `contexts/cockpit/`
- 現行モジュール: `domain/cockpit_geometry.py` `cockpit_layout.py` `cockpit_membership.py`
  `attention.py` `agent_activity.py` `agent_discovery.py` `pane_resolver.py`
  `session_naming.py` `session_boundary.py` `grouped_*.py` `presentation_adapter.py`
  `presentation_grouping/`、`application/cockpit_*.py` `cli_cockpit.py` `cli_agents.py`
  `attention_projection.py` `*_attention_presentation_provider.py` `tmux_ui.py`
  `grouped_detail.py` `presentation_runtime.py`、`presentation_state.py`。
- 役割: pane-centric cockpit semantics / attention state / presentation projection。
- adapter seam: presentation adapter (`plugin-ready-adapter-boundary.md`)。
- layer purity 注記: `agent_discovery` / `managed_marker` / `pane_resolver` が
  `tmux_client` に依存している。context 内に runtime adapter サブ境界を設け、pure
  derivation と tmux IO を分ける follow-up を context 内 issue で扱う。

### 6. redmine-governance context

- 候補配置先: `contexts/governance/`
- 現行モジュール: `redmine_context.py` `redmine_credentials.py`、`domain/ticket_adapter.py`、
  `infrastructure/redmine_ticket_provider.py`、`docs_tools/` (catalog governance)、
  `domain/module_health.py` `domain/module_registry.py`、
  `application/cli_module_health.py` `commands_module_health.py`
  `cli_docs_scaffold.py` `commands_docs_scaffold.py`。
- 役割: Redmine 読み取り context、ticket adapter、docs catalog governance、module-health
  gate (`module-health-gate.md`)。
- adapter seam: ticket adapter (read-only-by-design な `redmine_context`) / catalog
  backend。

### 7. scaffold context

- 候補配置先: `contexts/scaffold/` (もしくは現行 `scaffold/` を context root に昇格)
- 現行モジュール: `scaffold/` (canonical / rules / presets / canonical_sources)、
  `application/instruction_install.py` `instruction_doctor.py` の scaffold 部分。
- 役割: preset 配布 / canonical render / drift gate (`scaffold-rules.md` /
  `scaffold-distribution-minimization.md`)。
- 注記: `scaffold/presets/**` / `scaffold/canonical_sources/**` は packaging
  (pyproject `package-data`) と強結合のため、ディレクトリ名変更は最小化する。

### 8. release context

- 候補配置先: `contexts/release/`
- 現行モジュール: `application/release.py` `cli_release.py`、`instruction_*` の release 部分。
- 役割: release gate plan / bump / publish (`release-flow.md` /
  `release-helper-contract.md`)。

### 9. plugin-adapter context

- 候補配置先: `contexts/plugin/`
- 現行モジュール: `domain/plugin_manifest.py` `domain/provider_registry.py`、
  `application/provider_runtime.py`、各 context の adapter seam 集約点。
- 役割: external plugin API 公開前の built-in adapter 分類正本
  (`plugin-ready-adapter-boundary.md`) を実装側で受ける境界。
- 注記: provider seam は staged (pure fail-closed core seam + injected transport)。
  live network/write は per-task-review follow-up に残す。

### CLI / application composition

- `application/cli.py` (`build_parser()` / `main()`) と各 `cli_*.py` / `commands*.py`
  は **composition root** であり、特定 context の所有物ではない。context 別の parser /
  command handler を各 context へ寄せた後も、top-level CLI は registry composition として
  残す (`refactor-split-strategy.md` の CLI parser layer 方針と一致)。
- pyproject entry point `mozyo-bridge = "mozyo_bridge.application.cli:main"` は
  public surface。`cli:main` の import path は **移行の最後まで固定する**。

## Impact Evaluation (import path / CLI / public surface)

### Import path risk

- 最大リスクは top-level loose module (`state_store` 等) と `application.commands` の
  import path 変更。tests / downstream が `mozyo_bridge.application.commands.cmd_*` や
  `mozyo_bridge.state_store` を patch / import している。
- 緩和策: 移動先に実体を置き、旧 path から re-export する **facade** を残す。facade の
  retirement は `fallback-retirement-ledger.md` に台帳化し、別 issue で行う
  (本計画では撤去しない)。

### CLI surface risk

- `--help` / subcommand choices / default / `dest` / `func` binding は public CLI surface。
- 緩和策: context へ寄せる前に characterization test (representative `--help` substrings、
  choices / defaults、deprecated alias 警告、exit code) で pin する
  (`refactor-split-strategy.md` の Characterization Strategy 準拠)。

### Public API surface risk

- `mozyo_bridge.__init__` は `__version__` のみ公開。package import surface は薄い。
- entry point `cli:main` を固定すれば、外部から見える surface は変わらない。
- record 型 (`IssueRef` / `JournalRef` / `DeliveryOutcome` 等) を context 間 contract と
  して安定させる。provider-neutral 命名を維持する。

### Packaging risk

- `scaffold/presets/**` `scaffold/canonical_sources/**` は pyproject の
  `package-data` glob と結合。ディレクトリ rename は build 出力に直結するため避ける。
- catalog (`.mozyo-bridge/docs/catalog.yaml`) と
  `file_conventions.generated.yaml` の canonical_path 参照は doc 移動時に更新が必要。
  本 doc を含むため、追加後に `mozyo-bridge docs generate-file-conventions` /
  `--check` / `docs validate` を回す。

## Staged Migration Units (no bulk move)

各 unit は独立 issue として実装可能で、低リスク順に並べる。各 unit は
behavior-preserving move-only commit を基本とし、move と behavior change を分ける。

1. **Unit A — shared kernel / managed-state package 化 (低リスク)**
   - top-level loose module (`state_store` `managed_events` `session_inventory`
     `otel_store` `workspace_registry` `workspace_defaults` `presentation_state`) を
     `core/state/` package へ移し、旧 path facade を残す。
   - 影響: import path のみ。behavior 不変。characterization は import smoke + 既存
     unit test。**#12493 の最有力候補。**
2. **Unit B — release context 切り出し (低リスク)**
   - `release.py` `cli_release.py` を `contexts/release/` へ。tmux live behavior と遠く、
     release helper contract が境界を明文化済み。
3. **Unit C — governance / docs catalog context (低〜中リスク)**
   - `docs_tools/` `module_health` `module_registry` と関連 CLI を governance context へ。
     catalog generator / coverage gate を回す。
4. **Unit D — scaffold context root 昇格 (中リスク)**
   - `scaffold/` を context root として扱う。packaging glob を壊さないことを最優先。
     `scaffold status` / `scaffold canonical --check` で検証。
5. **Unit E — handoff / coordinator context (中〜高リスク)**
   - handoff / notify / sublane callback。monkeypatch target が `application.commands` に
     集中するため facade 必須。characterization を厚くしてから。
6. **Unit F — cockpit context + runtime adapter サブ境界 (高リスク)**
   - cockpit / attention / pane resolver。tmux IO と live layout に絡む。最後に切る。
     `domain → tmux_client` の purity 修正はこの context 内 follow-up。
7. **Unit G — plugin-adapter context 集約 (設計依存)**
   - provider seam を plugin context へ集約。staged seam 方針を維持。

## Files NOT Moving (理由と候補配置先)

| file / path | 移動しない理由 | 将来候補 |
| --- | --- | --- |
| `__init__.py` `__main__.py` | package root entry。`__version__` / `python -m` 入口で surface 固定 | root のまま |
| `application/cli.py` (`main`/`build_parser`) | pyproject entry point の固定先。composition root | root composition のまま |
| `shared/errors.py` `paths.py` `name_compat.py` | 既に最下層 kernel。移動は循環リスクのみ増やす | `shared/` 維持 |
| `scaffold/presets/**` `scaffold/canonical_sources/**` | pyproject `package-data` glob と強結合。rename = build 破壊 | `scaffold/` (context root 昇格は in-place) |
| `application/commands.py` (facade) | 多数の patch target / re-export 元。即時移動で test 破壊 | facade を残し family を順次外出し |
| `domain/pane_resolver.py` `agent_discovery.py` `managed_marker.py` | cockpit context 行きだが `tmux_client` 依存。purity 修正と同時移動は高リスク | cockpit context + runtime adapter サブ境界で分離 |
| `infrastructure/tmux_client.py` | safety-critical tmux send path。`tmux-send-safety-contract.md` 準拠。最初に触らない | `infrastructure/` 維持 (runtime adapter) |
| `.mozyo-bridge/docs/file_conventions.generated.yaml` | generator 出力。hand-edit 禁止 | catalog 変更 → 再生成 |

## Follow-up Scope (#12493) と除外

- **#12493**: 本計画の最初の低リスク実装単位。**Unit A (shared kernel / managed-state
  package 化)** を behavior-preserving move-only で実装することを推奨。import path facade
  を残し、retirement は別 issue で台帳化する。
- **除外 — #12468 `doctor.py` module-health drift**: `application/doctor.py` の module-health
  baseline drift は本 bounded-context 移行の対象外。`doctor.py` の line-count 是正は
  `module-health-gate.md` 系の独立 issue で扱い、本計画の move-only 方針に混ぜない
  (move commit に behavior/baseline 変更を入れない原則)。

## Feature-slug pilot record (#12570)

US #12570 (parent Feature #12533) は、Redmine Epic/Feature 順序を source/test layout へ
反映する Feature-slug 形 `features/<epic_slug>/<feature_slug>/` を、`execution_platform`
big-box の 1 module で behavior-preserving に実証した (base `13303db`)。番号順序の正本は
`bounded-context-map.md` の mapping metadata、命名規約・renumber 方針も同 doc。

実装済み pilot slice (move-only, facade 維持):

- source: `domain/delegation_route_executor.py` (the live executor; #12556/#12546 path) を
  `features/execution_platform/delegated_coordinator_nested_handoff/delegation_route_executor.py`
  へ移動。旧 path `mozyo_bridge.domain.delegation_route_executor` は #12493 と同一の
  **`sys.modules` facade idiom** で同一 module object を re-bind (attribute / monkeypatch 等価)。
- tests: 1:1 unit test を `tests/unit/execution_platform/delegated_coordinator_nested_handoff/`
  へ移動し、ROOT bootstrap を `parents[3] → parents[4]` に bump (#12490 mechanics)。
- catalog: facade split のため変更不要。旧 facade と新 module は共に catch-all
  `fc-package-source` (`src/mozyo_bridge/**/*.py`)、移動 test は `fc-tests` (`tests/**`) に乗る
  (`generate-file-conventions --check` / `docs validate --check-file-coverage` green で確認)。

**意図的に deferred (本 pilot に含めない)**:

- 残りの feature cluster (`delegation_route_planner` / `delegation_route_records` /
  `route_identity_ledger` / `delegation_project_config` / `delegation_projection` /
  `delegation_display` / `delegation_launch_adopt` / `delegated_coordinator_route_plan` /
  `grandchild_*`) の移動。expand 判断は #12570 j#65077 decision #6 に従い pilot 合格後に行う。
  これらは 6 件の live lane (12547/12549/12550/12553/12557/12561) が現に編集中のため、移動は
  main-merge 競合面を増やす。本 pilot は **isolated #12570 branch** で完結し in-flight branch に
  触れない (競合は main-merge 時の関心事。#12565 main integration は #12570 の後ろに held)。
- cross-module の `test_delegation_route_integration_readiness.py` (integration) は executor 単独の
  1:1 test ではないため移動せず、facade 経由で従来 path のまま green。cluster 全体移動時に追従する。

## Non-Goals

- 一括 (mega) リファクタブランチ。
- move commit 内の behavior 変更 / module-health baseline 変更。
- `domain → infrastructure` layer purity の全面修正を context 移行と同一 commit で行うこと
  (context 内 follow-up に分離)。
- legacy import path の fallback 無し撤去 (`fallback-retirement-ledger.md` 経由のみ)。
- `scaffold/presets/**` の package-data glob を壊す rename。
- pyproject entry point `cli:main` の import path 変更 (移行の最後まで固定)。
- arbitrary plugin loading / 新規 plugin system の発明 (`plugin-ready-adapter-boundary.md`
  の built-in adapter 分類に従う)。
