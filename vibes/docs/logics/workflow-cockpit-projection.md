# Workflow-State Cockpit Projection

Redmine #12674 (parent #12656 / #12670 roadmap; Version #297)。workflow runtime state (#12671 next_action / #12672 watcher / #12673 role-provider binding / #12857 stateful runtime) を Cockpit UI へ **read-only な projection** として見せるための read model と責務境界の設計正本。

> 本 doc は **projection の設計境界 (read model / boundary)** を固定するものであり、いずれの層の source-of-truth でもない。UI に出す行は Redmine journal / `workflow-runtime.sqlite` / live tmux から導出した派生値であって、workflow truth を新たに権威化しない。既存の cockpit 表示モデルとの関係は [[logic-unit-target-model]] (ProjectGroup → Unit → Target の display 階層) と [[logic-cockpit-attention-state]] (attention_state の派生 projection) が正本であり、本 doc はそれらを置き換えず、workflow next_action 層を **additive に上乗せ** する。本 doc とそれら canonical doc が矛盾した場合は canonical doc を優先し、本 doc を是正する。正本分離は [[rule-llm-rule-authoring]] `## 正本分離` に従う。

## 背景

#12671 → #12672 → #12673 → #12857 で、workflow runtime の pure-domain 層が揃った。

- `workflow_runtime.evaluate_workflow_runtime` が durable journal event を replay して `WorkflowRuntimeState` (lane ごとの `LanePendingAction` と overall `NextAction`) を導出する。
- `workflow_next_action.derive_workflow_next_action` がそれを `WorkflowNextAction` に enrich する (`owner_role` / `provider` / `route_identity` / `anchor` / `suggested_command` / `risk_level` / `requires_confirmation` / `blocked_reason`)。
- `redmine_event_intake.evaluate_event_intake` が watcher の入口として journal marker を取り込み、上記を `EventIntakeOutcome` に束ねる。
- `role_provider_binding.RoleProviderBinding` が role → provider を解決する。
- `core/state/workflow_runtime_store.WorkflowRuntimeStore` が events / route identities / advisory meta を `workflow-runtime.sqlite` に永続化する。

これだけ揃うと、operator は cockpit を見たときに「いまどの lane / issue を誰 (owner_role) がどの provider で持っているか」「どの Redmine anchor が次の action を待っているか」「blocked なのか」を一望したくなる。一方で Cockpit は既に [[logic-cockpit-web-ui]] の `/api/units` で五層 (tmux 行存在+stale / OTel activity / Redmine 段階4 join / attention 派生 / observation freshness) の join projection を持つ。ここに workflow next_action を **第六の additive join 層** として乗せるのが本設計の対象である。

ただし `## 非目標` のとおり、UI を先行実装して workflow truth を UI 側へ移すことはしない。state model と責務境界を先に固定する。

## 結論

```text
Truth / liveness 層 (本 doc では一切変更しない):
  Redmine journal / status   -> durable external memory (gate anchor / owner approval / completion)
  workflow-runtime.sqlite    -> workflow runtime state (events / route identities / advisory meta)
  live tmux                  -> liveness evidence (pane 存在 / process / cwd)

Projection 層 (本 doc が定義する):
  WorkflowProjectionRow      -> 上記から導出した display-only な行。routing / approval / close authority を持たない
```

`WorkflowProjectionRow` はどこか 1 つの mutable field に保存しない。`workflow-runtime.sqlite` の advisory meta や presentation current table に **latest derived value cache** を置いてよいが、その cache は `WorkflowRuntimeStore` の read からいつでも再計算できなければならない (cache であって正本ではない)。

## 投影 read model

cockpit に出す行は `WorkflowProjectionRow` という projection record として扱う。各 field は既存の pure-domain VO から **そのまま写す** だけで、本 doc は新しい workflow 語彙を作らない。

```yaml
workflow_projection_row:
  # --- Unit / lane identity (display 用、routing authority ではない) ---
  workspace_id: string            # Unit identity (unit-target-model)
  lane_id: string                 # 既定 "default"。lane 軸は workspace と別軸
  target_issue: string            # workflow_next_action.WorkflowNextAction.target_issue
  # --- workflow state (受け入れ条件が要求する表示項目) ---
  owner_role: string              # WorkflowNextAction.owner_role (workflow_runtime ROLE_*)
  provider: string                # WorkflowNextAction.provider (role_provider_binding 解決)
  role_provider: string           # "<owner_role> via <provider>" (format_role_via_provider)
  next_action: string             # WorkflowNextAction.action (workflow_runtime ACTION_*)
  state_class: string             # LanePendingAction.state_class (workflow_fill_decision LANE_STATE_*)
  blocked_reason: string          # WorkflowNextAction.blocked_reason (BLOCKED_* / FAILED_ROUTE_AMBIGUOUS)
  anchor: string                  # WorkflowNextAction.anchor (Redmine issue/journal pointer)
  # --- 補助 (情報であって権限ではない) ---
  route_identity: string          # WorkflowNextAction.route_identity (public-safe pointer。pane id ではない)
  suggested_command: string       # WorkflowNextAction.suggested_command (情報。実行は live preflight 経由)
  risk_level: string              # WorkflowNextAction.risk_level (RISK_*)
  requires_confirmation: bool     # WorkflowNextAction.requires_confirmation
  # --- liveness (live tmux から、別レイヤ) ---
  liveness: live | dead | unknown # Target の live preflight 観測。workflow state とは別軸
  observed_at: ISO8601
```

受け入れ条件が挙げる `owner_role` / `provider` / `lane` / `next_action` / `blocked_reason` / `anchor` はすべて `WorkflowNextAction` (および lane の `LanePendingAction`) が既に持つ field の写しであり、projection は **fold の出力を serialize する** 以上のことをしない。

### fold の入口 (再導出可能性)

projection は新しい計算路を持たず、既存の fold entry point を読むだけにする。

```text
WorkflowRuntimeStore.read_events()            -> LaneEvent[]
WorkflowRuntimeStore.read_route_identities()  -> RouteCandidate[] (issue ごと)
WorkflowRuntimeStore.read_meta()              -> ready_independent / ready_overlapping / capacity / owner_or_release_gate
        │
        ▼ evaluate_workflow_runtime(events, **meta)      = WorkflowRuntimeState (lane_actions + admission + next_action)
        ▼ derive_workflow_next_action(state, issue_routes=, issue_anchors=, binding=)  = WorkflowNextAction
        ▼ (watcher 経路では evaluate_event_intake(...) が上記を EventIntakeOutcome に束ね、
           intake / pending_action / workflow を一括 payload 化する)
        │
        ▼ WorkflowProjectionRow[] (lane ごと + overall 1 行)
```

`workflow-runtime.sqlite` の row ↔ domain VO の写しは application 層が行う (`WorkflowRuntimeStore` は f_140 の型を import しない bounded-context 境界を維持する)。projection はこの application 層の出力 (`*.as_payload()` の安定 string key) を読むだけで、行を独自に再 fold しない。

## 正本境界

```text
workspace / lane identity   -> registry.sqlite + workspace anchor (unit-target-model)
workflow runtime state      -> workflow-runtime.sqlite (events / routes / meta; advisory)
gate anchor / owner approval / completion -> Redmine journal / status
runtime liveness            -> live tmux observation
workflow_projection_row     -> 派生 projection (display only)
tmux / cockpit / color      -> projection only
```

- Redmine は workflow gate / completion の正本だが、tmux liveness の正本ではない。
- `workflow-runtime.sqlite` は workflow runtime state (replay 可能な advisory state) の正本だが、owner approval / close gate の正本ではない (それは Redmine)。`last_seen_pane_id` は cache/evidence 列であって routing authority ではない。
- live tmux は liveness の正本だが、owner approval / review state の正本ではない。
- projection はこの三層を **読む** が、どれかへ責務を寄せない。三層が矛盾したら `## 導出優先順位 / fail-safe` に従って安全側へ倒し、healthy を捏造しない。

## 投影に含めてよいもの

- `WorkflowNextAction` / `LanePendingAction` / `WorkflowRuntimeState` が既に持つ field の写し (`owner_role` / `provider` / `next_action` / `state_class` / `blocked_reason` / `anchor` / `route_identity` / `risk_level` / `requires_confirmation` / `suggested_command`)。
- Unit identity (`workspace_id` / `lane_id` / `target_issue`) と public-safe な display label。
- liveness 観測 (`live` / `dead` / `unknown`) を workflow state とは **別 field** として併記すること。
- 再計算可能な latest derived value cache。
- generic な state / reason 語彙 (workflow runtime / fill-decision の literal token)。

## 投影に含めないもの

- routing / handoff target identity (pane_id を UI JSON / HTML に出さない; routing は Target の live preflight が決める)。
- owner approval / review / close / completion の **正本**。projection は Redmine anchor への pointer (`anchor` / `route_identity`) を持つだけで、approval state を mozyo DB へ複製して正本化しない。
- Redmine の issue subject / journal 本文 / credential / 個人情報 ([[logic-cockpit-web-ui]] の Redmine join が subject を載せない privacy 境界を継承する)。
- private project の lane naming policy / operator 固有配色 / iTerm profile / 社内 escalation policy。
- workflow truth を pane の色 / title だけに保存すること (色は派生 projection の補助)。

## 既存 cockpit projection との関係

### Unit への結線 (key は pane_id ではなく issue / lane)

[[logic-cockpit-web-ui]] の `/api/units` 既存五層は **pane_id keyed** (各 pane に additive field を付ける) である。workflow runtime state は **issue / lane anchored** であり、pane が死んでいても state は残る。したがって workflow projection 層は pane_id ではなく **Unit identity `(workspace_id, lane_id)` と `target_issue`** で join する。

```text
ProjectGroup (display grouping)
  └─ Unit (workspace + lane + governance + role set)         <- unit-target-model
       ├─ Target (role-specific live pane; liveness 軸)       <- live preflight authority
       └─ WorkflowProjectionRow (target_issue の next_action) <- 本 doc が乗せる additive 層
```

Unit に live pane が無い (Target dead) 場合でも、`workflow-runtime.sqlite` に当該 lane/issue の event があれば row は `liveness: dead` として残す。これは [[logic-cockpit-attention-state]] の「Redmine が `review_request` でも pane が死んでいるなら `review_waiting` + reason `target_dead`」と整合する扱いである。

### attention_state との分担 (重複しない)

[[logic-cockpit-attention-state]] の `attention_state` (`owner_waiting` / `review_waiting` / `blocked` / `stalled` / `done` / `retired_candidate` / `unknown`) は **coarse な triage signal** (「どの pane を先に見るか / どれくらい急ぐか」) である。本 doc の `WorkflowProjectionRow` は **fine-grained な next-action carrier** (「次の具体的 action は何か / owner_role via provider は誰か / どの anchor か」) である。両者は同じ durable source を読むが display 役割が異なる。

```text
attention_state (cockpit-attention-state)   -> 注意度 (triage)。severity / reason_code を持つ
workflow_projection_row (本 doc)            -> 次 action の中身。owner_role / provider / anchor / command
```

- 二つは **drift してはならない**: どちらも workflow truth (Redmine + workflow-runtime.sqlite + tmux) を読み、`attention_state` の `review_waiting` と projection の `next_action == perform_review` は同じ state class から導出される。実装時は両 projection が同一 fold (`evaluate_workflow_runtime` / `derive_workflow_next_action`) を共有し、別計算路で乖離しないことを test で pin する ([[logic-cockpit-attention-state]] `## Verification Notes` と同じ姿勢)。
- workflow projection は `attention_state` を **置き換えない**。将来 `attention_state` の `review_waiting` / `owner_waiting` / `blocked` 導出を本 read model から pin できる (cockpit-attention-state の `## Implementation Split` task 5) が、それは follow-up であり、本 doc は読み筋を固定するだけで attention derivation を複製しない。

## watcher / next_action / provider binding との整合

受け入れ条件「watcher / next_action / provider binding と整合する」を、次の不変条件で満たす。

- **next_action (#12671)**: projection の `owner_role` / `next_action` / `route_identity` / `anchor` / `suggested_command` / `risk_level` / `requires_confirmation` / `blocked_reason` は `derive_workflow_next_action` の出力をそのまま写す。projection 側で risk policy (`_ACTION_RISK`) や route 選択 (`_resolve_route` の last-write-wins) を別実装しない。
- **watcher (#12672)**: watcher 経路の `evaluate_event_intake` が出す `EventIntakeOutcome` (`intake` / `pending_action` / `workflow`) と projection は同じ `WorkflowCommandResult` を共有する。watcher の fail-closed (`FAILED_ROUTE_AMBIGUOUS`、`PENDING_NEEDS_CONFIRMATION`、suppressed dedup) は projection でも安全側に反映し、`route_ambiguous` を healthy に倒さない。
- **provider binding (#12673)**: `provider` / `role_provider` は `RoleProviderBinding.provider_for` / `format_role_via_provider` の解決値を写す。provider 語彙は open (`KNOWN_PROVIDERS` は advisory) なので、projection は未知 provider を表示できる必要があり、provider を closed allowlist で弾かない。binding が unbound を返したら `provider` は空のまま (fail-closed) にし、勝手に default 補完しない。

## 導出優先順位 / fail-safe

矛盾・読み取り不能時は安全側へ倒す ([[logic-cockpit-attention-state]] `## Derivation Priority` / [[logic-runtime-observability-boundary]] の fail-safe semantics に準拠)。

```yaml
fail_safe:
  blocked_reason 優先:
    - blocked_reason (unknown_action / route_identity_unresolved / route_ambiguous) は
      healthy / ready に倒さない。derive_workflow_next_action / classify_pending_action の
      precedence をそのまま写す
  liveness は別軸:
    - workflow state が present で pane dead -> row を残し liveness: dead を併記する。
      pane 不在を理由に workflow row を消さない
  source 読めない / 矛盾:
    - workflow-runtime.sqlite 不在 / schema 不一致 (WorkflowRuntimeStoreError) -> 当該 Unit を
      healthy にせず、unknown 相当 (degraded) として表示し source を読みに行く誘導を出す
  advisory:
    - WorkflowRuntimeState.advisory は True。projection は advisory を side-effect / 自動実行の
      根拠にしない。action は action-time live preflight (cockpit-web-ui の POST gate) を必ず通す
```

## WebSocket / live update scope split

受け入れ条件・設計思想のとおり、**先に state model を固め、WebSocket / live push は最後に検討する**。本 doc の v1 は次に限定する。

- 既存 cockpit と同じ **explicit reload + action-time live preflight** モデルを踏襲する ([[logic-cockpit-web-ui]] / grouped reload UX)。polling / push / sidecar / background observer を本 layer のために増やさない。
- projection は read-only。表示中 snapshot を refresh するだけで workflow gate を動かさず、side effect を authorize しない。
- WebSocket / live update / server-push は `## 将来設計への判断材料` 行きとし、state model と responsibilities が固まってから別 issue で検討する。

## public / private boundary

OSS default は generic な state 名と projection hook だけにする ([[rule-public-private-boundary]] 準拠)。

- 入れてよい: workflow state / fill-decision の literal token、role / provider token、`anchor` / `route_identity` の public-safe pointer、JSON / text 出力、控えめな display label。
- 入れてはいけない: private Redmine project 名 / 個人名 / 絶対 path / pane id / credential / prompt 本文、operator 固有配色、iTerm profile、社内 escalation policy。

## Anti-patterns

- UI / projection を workflow truth の正本にする。
- workflow runtime state を pane_id で keyed にして、pane 消滅で issue/lane の workflow row を失う。
- projection を routing / approval / review / close authority に使う。
- `attention_state` と workflow projection を別計算路で育てて drift させる。
- Redmine gate / owner approval / completion を `workflow-runtime.sqlite` へ複製して正本化する。
- provider を closed allowlist で弾き、`grok` のような未知 provider binding を表示できなくする。
- `route_ambiguous` / `unknown_action` / source 読めずを healthy に倒す。
- `last_seen_pane_id` (cache 列) を handoff target の正本にする。
- private cockpit composition / operator policy を OSS default に焼く。

## 実装分割

[[logic-cockpit-attention-state]] と同じく、本 task は **design doc で完了** する。実装は別 issue に分割する (UI 先行・workflow truth 移譲を避けるため)。

1. application 層に `WorkflowRuntimeStore` read → `WorkflowProjectionRow[]` の pure projection read model を追加する (fold 再利用、独自再計算なし)。
2. `/api/units` (または additive な `/api/workflow` field) に Unit-keyed な workflow projection 層を付ける。pane_id identity / 既存五層を変更しない (additive)。
3. cockpit display / CLI (`agents targets` / future `cockpit status`) に owner_role / provider / next_action / blocked_reason / anchor 列を出す。
4. `attention_state` の `review_waiting` / `owner_waiting` / `blocked` 導出を本 read model から pin する (cockpit-attention-state task 5 と接続)。
5. WebSocket / live update を別 issue で検討する (state model 確定後)。

各 task は runtime / tests を伴うため Claude implementer lane に回し、Codex が review する。特に projection が routing / handoff safety gate を触らず、workflow truth を UI へ移さないことを test で固定する。

## scope 境界 / Design Consultation triggers

- workflow projection を **action authority** にしたくなった (UI から workflow gate を直接進める) 場合は本 layer で実装せず、action-time live preflight / workflow command surface ([[logic-workflow-step-command-design]]) へ escalate する。
- `attention_state` 語彙の拡張や severity 体系の変更が必要になった場合は [[logic-cockpit-attention-state]] 側の Design Consultation で扱い、本 doc に複製しない。
- workflow runtime の DB current table 境界に踏み込む場合は [[logic-unit-presentation-state-db]] / [[logic-managed-state-model]] を正本とする。

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `git diff --check`
- `mozyo-bridge docs resolve vibes/docs/logics/workflow-cockpit-projection.md --repo . --format text` で関連 canonical docs 解決を確認する。
