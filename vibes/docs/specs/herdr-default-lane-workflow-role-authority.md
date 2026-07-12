# Herdr default-lane durable workflow-role authority (Redmine #13583)

純 herdr セッションの **default lane pair**（coordinator の Codex + その Main-unit assistant
Claude）が、`grandparent_coordinator`（department root）と `project_gateway` を step 時点で
区別する durable な workflow-role authority を持たず、`workflow step --dry-run --json` が設定
完了後も `ambiguous_default_coordinator_role` で fail-closed する問題（#13581 j#75707）を解く
contract。

この doc は `logic-workflow-step-command-design` の §#13489（herdr default-lane role 解決）が
「default lane は durable role authority が無いため fail-closed」と定義した空白を埋める durable
authority の正本であり、`spec-herdr-native-identity`（mzb1 identity model / default lane 意味）と
`logic-ticketless-project-gateway-runtime-ux`（grandparent vs project_gateway の役割境界と一段
委譲）を前提にする。Design Consultation は Redmine #13583 j#75780、Answer は j#75782、独立検証
verdict は j#75808。

## 1. 原則（provider / placement を role authority に昇格しない）

- mzb1 の `role` segment は runtime **provider** token（`claude` / `codex`）であり workflow role
  ではない（`spec-herdr-native-identity` §1）。default lane であること・pane 配置・provider を
  workflow-role authority へ昇格しない（`logic-workflow-step-command-design` 設計原則4）。
- caller role は **explicit な durable binding** からのみ解く。binding が無い lane は既存分類を
  維持する（通常 non-default: codex→`delegated_coordinator` / claude→`implementation_worker`；
  binding 無し default → 従来どおり `ambiguous_default_coordinator_role`）。
- missing / duplicate / invalid / provider-mismatch は fixed reason + `next_owner=operator` で
  fail-closed する。

## 2. Storage（Q1-A: portable tracked static artifact）

durable role binding は repo-local static artifact `.mozyo-bridge/workflow-role-bindings.json`
に置く。これは `logic-managed-state-model` の repo-local static-artifact 境界に従う **topology
宣言**であり、runtime state ではない。

- 保存する: schema discriminator / version / 各 binding の canonical role / project scope /
  advisory `source_pointer`（review 用の durable pointer、route authority ではない）。
- 保存しない: `workspace_id` literal（registry 正本、read 時に current workspace anchor を独立
  attestation として合成する）、liveness / delivery / approval / current-status / credential /
  locator。
- `lane_id` は **保存しない**。derived（§3）。冗長保存による drift を避ける。
- `.mozyo-bridge/config.yaml` の authority 禁止境界を維持し、専用 core-owned loader のみが読む。

schema（version 1）:

```json
{
  "schema": "mozyo.workflow-role-bindings",
  "version": 1,
  "bindings": [
    {"role": "grandparent_coordinator", "source_pointer": "redmine:#13583"},
    {"role": "project_gateway", "project_scope": "cloud-drive-management",
     "source_pointer": "redmine:#..."}
  ]
}
```

canonical role 語彙は `grandparent_coordinator | project_gateway`（closed）。旧 `root_coordinator`
は **compat 入力 alias** として `grandparent_coordinator` へ正規化するが、新規正本語彙へは書かない。

schema は **closed** かつ **exactly typed** で fail-closed に検証する: top-level key は
`{schema, version, bindings}` のみ、`schema` は exact literal string（whitespace padding / 非 string を
受理しない）、`version` は exact int（JSON `true` / `1.0` は Python loose equality で `== 1` になるため
bool / float を明示排除）、`bindings` は present な list 必須（欠落 / null は invalid、空 topology は
明示 `[]` のみ）、entry key は `{role, project_scope, source_pointer}` のみ、role は非空 string、
scope / source_pointer は key present なら string（explicit `null` は空 scope へ decay させず reject、
非文字列を暗黙 `str()` 変換しない）。present だが malformed な宣言は file 不在の fall-through と同一に
扱わず invalid にする。

## 3. Lane identity（Q2-A: root=default、gateway=deterministic project-scoped lane）

- effective runtime key = `(current attested workspace_id, binding lane_id, binding project_scope)`。
  workspace_id は upstream の sender-identity gate（`MOZYO_WORKSPACE_ID` 由来、fail-closed）で
  既に attest 済みで、file には持たない。
- `grandparent_coordinator` は default lane（`lane_id = "default"`, project_scope 空）。
- `project_gateway` の lane id は pure / versioned resolver `project_gateway_lane_id(project_scope)`
  で導出する。scope はまず whitespace trim で **canonicalize** し（`"scope"` と `"  scope  "` は
  設計上同一 lane）、scheme tag（`pgwv1`）+ **budget 内に bound した** readable slug + canonicalized
  scope の短い digest で構成する。保証は (a) 決定論的、(b) machine 間で安定、(c) project-scope lane
  uniqueness は **48-bit collision-resistant digest** が担い、相異 scope の衝突は天文学的に稀だが
  provably impossible ではない。**同一 declaration 内**に衝突する 2 binding が居る場合は parse 時
  slot-collision で fail-closed される（availability failure であり misroute ではない）。ただし
  `resolve_role_for_lane` は current declaration の derived `lane_id` 一致のみで照合するため、
  **cross-revision alias**（revision A が scope X で lane を mint → 物理 lane 存続 → revision B が別
  scope Y かつ digest 衝突で同一 lane id を宣言）は parse collision が発火せず、既存 lane が Y として
  解釈され得る。これは 48-bit residual risk として残り、本 contract は **never-misroute を保証しない**。
  slug は digest と独立なので切詰めは衝突挙動を変えない、(d) `pgwv1_` prefix により `default` へ decay
  しない（構造保証）、(e) mzb1 assigned-name（`NAME_MAX_LENGTH=128`、非 `[A-Za-z0-9]` は 3 char escape）
  へ実 ws + provider と合成しても収まる。empty scope は fail-closed。provable never-misroute / injectivity
  （full-scope identity / migration fence）を要求する場合は別 Design Consultation とする。
- project_gateway と root grandparent は同一 slot を奪い合わない: grandparent は default、gateway は
  derived lane。同一 lane_id を導く 2 binding は slot collision として fail-closed。

## 4. Provider 照合（provider_binding から別解決）

expected provider は既存 `provider_binding`（`role_provider_binding` / `.mozyo-bridge/config.yaml`）
から別途解決する。`grandparent_coordinator` は provider_binding の compat role `root_coordinator`、
`project_gateway` はそのまま `project_gateway` に map する（いずれも既定 codex）。現 lane の
provider が expected と一致しない、または expected が解決不能なら provider-mismatch で fail-closed。
provider binding / live inventory / project metadata は独立照合であり current-role authority では
ない。

## 5. Resolution matrix（pure）

| condition | status | reason (machine token) |
| --- | --- | --- |
| current lane_id に一致する binding が 1 件、provider 一致 | resolved | — |
| 一致 binding 無し | missing | —（caller は既存分類を維持）|
| 一致 binding 2件+（防御的; validation で slot collision は既に排除）| ambiguous | `herdr_role_binding_ambiguous` |
| declaration が malformed（schema/version/role/scope/slot）| invalid | `herdr_role_binding_invalid` |
| provider 不一致 / expected 解決不能 | provider_mismatch | `herdr_role_provider_mismatch` |

resolved 時、herdr `workflow step` は tmux / raw Herdr / raw pane へ fallback せず、role を名指す。
blocked（invalid / ambiguous / provider_mismatch）は fixed reason + `next_owner=operator`。

## 6. Increment 境界

- **Increment 1**（本 spec の実装）: schema / core-owned loader / pure role+lane resolver /
  docs / catalog / tests。**resolution-only**（`primitive=none`、send / lifecycle mutation なし）。
  resolved な grandparent / project_gateway は `herdr_role_resolved_forward_pending`（`no_op`）で
  role を名指し、一段委譲の forward transport は行わない。
- **Increment 3**（後続・mid-review 承認後）: grandparent→project_gateway consult /
  project_gateway→delegated_coordinator child-intake の Herdr-native 一段 forward SEND wiring。
  既存 tmux-era project-gateway primitive への個別 fallback は追加しない。

## 7. 実装 surface

- pure domain: `...f_140_delegated_coordinator_nested_handoff/domain/workflow_role_authority.py`。
- loader: `...f_140_delegated_coordinator_nested_handoff/application/workflow_role_authority_source.py`。
- herdr step 結線: `...domain/workflow_step_herdr.py`（`resolve_herdr_workflow_step` の
  `role_authority` 分岐）+ `...application/herdr_workflow_step.py`（`_resolve_role_authority`）。
- role token 正本: `...f_130_handoff_routing/domain/transition_role.py`
  （`grandparent_coordinator` / `project_gateway`）。
