# Runtime Observability Boundary

Redmine #12003。runtime observability を v0.8 の境界として整理し、Redmine durable
state、live tmux、tmux user option projection、OTel event store、future sidecar の責務を
分ける設計正本。

この文書は docs-only の判断材料であり、OTel / sidecar / UI の実装採用を確約しない。

Redmine #12223 (v0.9.4) で、runtime observation を **storage format 非依存の timestamped
snapshot** として定義する contract を追記した。snapshot の意味・vocabulary・term 制約・
follow-up issue への引き継ぎは `## Runtime Observation Snapshot Contract` に集約する。
本節は本 doc の Source-of-Truth Map / Responsibility Split / Attention Derivation Boundary を
**置き換えず、observation 品質の語彙層として上乗せする**。設計経緯は #12223 journal #61159
(Design Consultation) / #61162 (Implementation Request)。

Redmine #12227 (v0.9.4) で、continuous polling / push / sidecar / OTel formalization を v1 runtime
observation freshness scope から **分離** し、future workstream と接続条件を明確にした。v1 は
explicit reload (#12224) + action-time live preflight (#12226) を基本とし、polling / push / sidecar /
OTel formal adoption は future scope に置く。詳細・接続点・接続条件・方向制約は
`## Future Push / Sidecar Observer Scope Split` に集約する。設計経緯は #12227 journal #61218
(Start / Dispatch Decision) / #61220 (Implementation Request)、源流は #12196 journal #61156。

## Current Observation

2026-06-15 に repo-local `doctor --json` で確認した現状:

- OTel receiver: unreachable
- OTel store: absent / total events 0
- OTel unobserved agent panes: 22
- tmux-ui host wiring: not installed
- tmux section: warning
- scaffold / rules / skills: ok

この状態でも mozyo-bridge の handoff / target discovery / Redmine-driven workflow は
動ける。したがって、現時点で OTel や tmux-ui host wiring を core workflow の必須
source に昇格させるのは早い。

## Source-of-Truth Map

```text
workflow state          -> Redmine issue / journal / status
owner/review gates      -> Redmine durable record + governed workflow rules
workspace identity      -> registry.sqlite + workspace anchor
lane / checkout identity-> lane id / checkout id + workspace registry context
runtime liveness        -> live tmux observation
target preflight        -> live tmux + cwd/process/role/workspace/lane checks
activity signal         -> OTel/event store or future sidecar input, best-effort
attention state         -> derived record from workflow + runtime + activity inputs
tmux user options       -> projection cache only
color / title / UI      -> presentation projection only
```

## Runtime Observation Snapshot Contract

Redmine #12223 (parent #11825 Plugin / Adapter 境界設計, version #228 `v0.9.4 runtime
observation reload / freshness`)。runtime observation の意味を **storage format から切り離し**、
timestamped snapshot として定義する contract。

### Storage 非依存の core decision

runtime observation は **timestamped snapshot** としてモデル化する。SQLite / JSON / YAML /
JSONL / memory cache / event store のどこに置くかは persistence と query mechanics を変える
だけで、observation を current truth にはしない。「新しく書き込まれた」「永続化された」は
「今正しい」を意味しない。

storage backend の本格実装、sidecar / push observer 実装、public plugin API 化は本 contract の
**非目標** (それぞれ #12227 future scope、別 workstream)。本節は意味契約 (docs) のみを固定し、
runtime code の挙動は変更しない。

### Snapshot envelope vocabulary

runtime observation snapshot は最小 envelope として次の vocabulary を持つ。storage layout が
何であれ、この意味で解釈する。

```yaml
runtime_observation_snapshot:
  observed_at: ISO8601              # この observation を capture した時刻 (UTC)
  source: redmine | tmux | otel | sidecar | managed_event | cache | other
  method: live_query | command_boundary_event | reload | poll | projection_read | imported_event
  freshness: fresh | stale | expired | unknown
  readability: readable | unreadable | partial
  strength: authoritative_for_source | strong_runtime_signal | weak_observation | projection_only | unknown
  stale_reason: null | age_exceeded | source_unreadable | source_changed | reload_required | contradicted | unsupported_schema | missing_source
  contradiction: null | source_conflict | live_runtime_conflict | durable_record_conflict | internal_inconsistency
  source_refs:
    - redmine:#12196#61156
    - tmux:%pane@observed_at
    - managed_event:<id>
```

- `strength` は **observation method の強さ** を表す。global truth を含意しない。`confidence`
  を使ってもよいが、その場合も「observation method への confidence」であって「workflow
  completion への confidence」ではないと定義する。
- `source` / `method` は本 doc の Source-of-Truth Map / Responsibility Split と整合させる。
  例: `source: redmine` + `method: live_query` は durable workflow record の読み取り、
  `source: tmux` + `method: live_query` は live runtime liveness、`source: cache` /
  `method: projection_read` は projection-only。
- `source_refs` は snapshot が何を見て導出されたかの最小ポインタ。durable anchor (Redmine
  journal 等) を指せる場合は指す。

### Term restrictions (truth-like field の禁止)

runtime observation snapshot の **generic field** に truth 系の名前を使わない。source が適切な
durable / ACK source で、field 名がその source に scope されている場合のみ truth 系語彙を許す。

```yaml
forbidden_generic_snapshot_fields:    # snapshot envelope の汎用 field 名として禁止
  - completed
  - approved
  - current_status
  - delivered
  - accepted
allowed_scoped_alternatives:
  - redmine_status / workflow_gate:   # Redmine から読んだ場合のみ
  - delivery_ack_status:              # DeliveryOutcome / ACK contract から読んだ場合のみ
  - runtime_liveness:                 # live tmux / sidecar runtime source から読んだ場合のみ
  - attention_state:                  # derived projection としてのみ。source_refs 必須
  - snapshot_freshness / readability / strength:  # observation 品質を表す field
```

`completed` / `approved` / `delivered` / `accepted` は durable record + governed workflow
(Redmine) / ACK contract (`vibes/docs/logics/ack-completion-receiver-state.md`) の所管であり、
snapshot freshness から導出しない。`ack-completion-receiver-state.md` の「delivery ACK と task
completion は別物」「pane / stdout silence を completion truth に昇格させない」と同じ境界を、
snapshot field 名の層でも守る。

### Source boundary (再掲・本 doc 整合)

snapshot vocabulary は source boundary を上書きしない。Source-of-Truth Map / Responsibility
Split をそのまま継ぐ。

- workflow truth / review / owner approval / routing decision / close / task completion は
  **Redmine durable record + governed workflow rules** の正本。
- workspace identity は **registry + workspace anchor** の正本 (`managed-state-model.md`)。
- desired managed state は `mozyo` command boundary で event として記録できる (「mozyo が X
  した / X しようとした / X を mark した」)。これは managed event であって current liveness では
  ない (`managed-state-model.md`)。
- runtime liveness / pane existence / foreground process / active split / action target
  availability は **action-time live runtime observation** の正本。
- UI / cockpit / tmux user options / cache table は projection または diagnostic snapshot のみ。

### Freshness / fail-safe semantics

- stale snapshot は **visibly labeled stale** であれば diagnostics 用に表示してよい。
- stale / unreadable / contradictory な snapshot は `unknown` または `reload_required` を
  導出する。`healthy` を導出しない (Attention Derivation Boundary の Rules と同じ姿勢)。
- snapshot freshness は review / close / owner approval / completion / side-effecting routing の
  要件を **満たさない**。snapshot が fresh でも、それらの gate は durable record / live preflight
  が別途必要。
- durable workflow record と runtime snapshot が矛盾した場合、**新しい timestamp を全面的に
  信頼して** 解決しない。source responsibility (どの source がその field の正本か) で解決する。

### Contract handoff to follow-up issues

本 contract を前提に、後続 issue へ次を引き継ぐ。

```yaml
"#12224 reload command":
  - reload は diagnostic / display snapshot を refresh する
  - 出力に observed_at / freshness / readability / source / method を含める
  - reload は workflow truth を更新せず、action safety を含意しない
"#12225 cockpit UI":
  - UI は last refreshed / observed_at と stale / unreadable / unknown state を表示する
  - reload button は snapshot のみ refresh する
  - UI action は表示中 snapshot を信頼せず action-time preflight を実行する
"#12226 action-time preflight":
  - side-effecting command は実行時に live runtime observation を行う
  - snapshot は「どこを見るか」を示すだけで、行動許可ではない
  - stale / unreadable / contradictory snapshot は fail closed するか reload / live preflight を要求する
"#12227 future sidecar":
  - v1 は explicit reload + action-time live preflight を使う
  - polling / push / sidecar / OTel formalization は future scope
  - sidecar はより強い runtime / receiver-state signal を持てるが、workflow truth / task completion は持たない
  - 正本は本 doc `## Future Push / Sidecar Observer Scope Split` (#12227 で codify)
```

## Action-Time Live Preflight Boundary

Redmine #12226 (parent #11825 Plugin / Adapter 境界設計, version #228 `v0.9.4 runtime
observation reload / freshness`)。`## Runtime Observation Snapshot Contract` の snapshot 意味契約を
**side-effecting command の実行境界** に落とす節。設計経緯は #12196 journal #61156 / #12223
journal #61159。

本節は **新しい runtime 挙動を導入しない**。既存実装は既に「side-effecting command は実行時に
live runtime observation を行い、保存済み / 表示済み snapshot を行動許可にしない」を満たしている
(下記 inventory はその事実の棚卸し)。本節はその不変条件を docs に固定し、後続の reload (#12224) /
cockpit UI (#12225) / future sidecar (#12227) が snapshot を side-effect 許可に昇格させないための
境界を pin する。runtime code の挙動変更は本節の非目標であり、実装が本節と乖離した場合は実装側を
correction する。

### Core rule

- side-effecting command は **実行時 (action time) に live runtime observation** を行う。保存済み /
  表示済み snapshot (inventory.sqlite / managed-events / tmux user option projection / cockpit
  表示 / reload 出力) は「どこを見るか」を示すだけで、**行動許可ではない**。
- stale / unreadable / contradictory snapshot は side effect を authorize しない。command は
  **fail closed** するか、live preflight / reload を要求する。snapshot age から `healthy` /
  `current` を導出しない (`### Freshness / fail-safe semantics` と同じ姿勢)。
- snapshot freshness は side-effecting routing の要件を満たさない。snapshot が fresh でも、
  action-time の live preflight を別途行う (snapshot contract で明記済みの境界をそのまま継ぐ)。
- live observation が読めない / 矛盾する場合は `unknown` / `reload_required` を導出し、`healthy` を
  仮定して side effect を進めない。

### Side-effecting command inventory

side-effecting surface の棚卸し (2026-06-19 時点)。各 surface は mutate (keystroke 送出 / pane
kill / pane 移動 / window 作成 / user option 書込) の **前に** live tmux を再観測する。下表は
観測時点の事実であり、実装変更時は本表と実装を同期する (`src/mozyo_bridge/application/commands.py`
中心、`domain/pane_resolver.py` / `infrastructure/tmux_client.py` の primitive 経由)。

```yaml
keystroke / text entry:
  surfaces: [read, type, keys, message]
  action_time_live_observation:
    - resolve_target -> pane_lines (live list-panes でターゲット pane を解決)
    - require_read (直近 capture-pane 観測 = read marker 取得を keystroke の必要条件にする)
    - message: wait_for_text(marker) で capture_pane を polling し landing を観測 (未観測時 C-u rollback)
session / window create / rename:
  surfaces: [mozyo (bare), init]
  action_time_live_observation:
    - session_exists / list_session_windows / find_agent_window (live tmux) で create/adopt を決める
    - ensure_agent_target / wait_for_agent_terminal_pane で foreground process を live 確認
    - init: pane_location / pane_lines / session_exists の guard をすべて mutation の前に通す
cockpit layout actions:
  surfaces: [layout apply cockpit, cockpit (create/append/adopt/reset/rebuild/peer-adopt/rebalance/reconcile)]
  action_time_live_observation:
    - _read_cockpit_columns / _read_cockpit_geometry / _cockpit_session_present (live list-panes / has-session)
    - kill / join-pane / resize-pane / swap-pane の前に identity (managed marker) と geometry を live 確認
    - --confirm を要する破壊的 action は live preflight 後にのみ plan を execute する
handoff / notify:
  surfaces: [handoff send, handoff reply, handoff cross-workspace-consult, notify-*]
  action_time_live_observation:
    - pane_info -> resolve_target (live) + project_preflight_target で canonical identity に写像
    - tmux-send-safety-contract.md の Layer B deterministic preflight (window / session / active-split / foreground process)
    - wait_for_text(marker) による landing 観測 (strict standard では未観測時 C-u rollback / marker_timeout)
attention projection (write user option):
  surfaces: [agents attention-project --apply]
  action_time_live_observation:
    - discover_agents -> pane_lines (live) で対象 pane を列挙してから set-option を best-effort 書込
    - 書込先は projection cache (`@mozyo_attention_*`) であり workflow truth ではない
```

read-only diagnostic (例: `cockpit doctor-geometry`、`agents targets`、`doctor`、reload 出力) は
side effect を持たないため本境界の対象外だが、その出力 snapshot を別 command の side-effect 許可
として転用しない (snapshot は pointer)。

### Source boundary consistency (既存 contract を弱化しない)

本節は既存の send-safety / target preflight を **強化も弱化もしない**。それらを action-time live
preflight の構成要素として再宣言するだけである。

- **send-safety**: handoff / message / notify-* の Enter 発行条件は
  `vibes/docs/logics/tmux-send-safety-contract.md` の `## Default Delivery Promise (v0.4)` /
  `## Queue-Enter Default Rail` が正本。queue-enter の Layer B deterministic preflight 強度、strict
  standard fallback の marker 観測必須、blind Enter 禁止条件は本節で一切弱めない。default rail 化を
  根拠に preflight を緩めない。
- **target preflight**: pane existence / session / active-split / foreground process / cwd は
  `vibes/docs/logics/managed-state-model.md` の通り **live tmux 正本**。snapshot / projection から
  代替しない。これは #11666 の stale 誤判定を安全事故にしないための不変条件と同型。
- **desired managed state**: managed-events は「mozyo が X した / X しようとした」の事実であって
  current liveness ではない (`managed-state-model.md`)。side-effecting command の target liveness 判定に
  managed-events を liveness 正本として使わない。
- **durable workflow truth**: review / close / owner approval / routing decision は Redmine durable
  record の所管 (`## Responsibility Split`)。side-effecting command の preflight が live runtime を
  満たしても、それは workflow gate を満たさない (両者は別境界)。

### Non-goals

本節は次を許可しない (issue #12226 非目標と一致)。

- queue-enter safety の弱体化、または preflight signal の削減。
- inactive pane を default rail で side effect 対象に許可すること。
- snapshot を workflow truth / side-effect 許可に昇格させること。
- continuous polling / push / sidecar を action 前提にすること (explicit reload + action-time live
  preflight を v1 の前提とする。push / sidecar formalization は #12227 future scope)。

## Future Push / Sidecar Observer Scope Split

Redmine #12227 (parent #11825 Plugin / Adapter 境界設計, version #228 `v0.9.4 runtime
observation reload / freshness`)。runtime observation freshness v1 から **continuous polling /
push / sidecar / OTel formalization を分離** し、将来 workstream と接続条件を明確にする scope
契約。源流は #12196 j#61156 の split decision、前提は本 doc `## Runtime Observation Snapshot
Contract`。本節は docs-only の scope 固定であり、sidecar / OTel の実装採用や runtime code の
挙動変更を確約しない (非目標は末尾に再掲)。

### v1 採用範囲 (explicit reload + action-time preflight)

v1 runtime observation freshness は次の 2 機構を基本とする。継続的な背景観測 (polling / push /
sidecar / OTel formalization) を v1 の前提に **しない**。

```yaml
v1_baseline:
  explicit_reload:                # #12224 reload command
    動作: operator/UI が明示的に diagnostic / display snapshot を refresh する
    出力: observed_at / freshness / readability / source / method を含む (snapshot envelope)
    境界: workflow truth を更新せず、action safety を含意しない
  action_time_live_preflight:     # #12226 action-time preflight
    動作: side-effecting command は実行時に live runtime observation を行う
    境界: snapshot は「どこを見るか」を示すだけで行動許可ではない。stale / unreadable /
          contradictory snapshot は fail closed するか reload / live preflight を要求する
```

判断原則: **freshness は「明示 reload で取り直す」+「行動直前に live で確かめる」で担保し、
背景で常時 push してくる observer を v1 の必須 source にしない**。これは `## Decision` の
「OTel を formal source of truth にしない」「best-effort observer input のまま扱う」姿勢と同じ
延長線上にある。

### Future scope に分離する範囲

次は v1 の prerequisite ではなく、独立 workstream の future scope に置く。

```yaml
future_scope:
  continuous_polling:
    強化点: snapshot を定期取得し freshness 劣化を自動で縮める
    v1_に不要な理由: explicit reload + action-time preflight で freshness 要件は満たせる。
                     polling 常時化は cost と stale-as-healthy 誤判定 risk を増やす
  push_observer:
    強化点: source 側から変化を push し reload 待ちの latency を縮める
    v1_に不要な理由: push 経路は best-effort で受信漏れ = unknown。v1 は受信漏れ時に
                     reload_required / unknown へ縮退できれば足り、push は freshness の
                     必須前提にならない (`managed-state-model.md` の OTel push と同型)
  sidecar_observer:
    強化点: agent process を外側から包み、rendered text に依存しない machine-readable な
            receiver runtime signal (runtime.input.ack / process.exited / output.eof 等) を持つ
    v1_に不要な理由: sidecar は `mozyo_bridge_pty` worktree の独立 workstream。現行
                     `mozyo-bridge` codebase に sidecar 実装は存在しない。v1 は tmux + durable
                     record で動く (`ack-completion-receiver-state.md`)
  otel_formal_adoption:
    強化点: OTel を stable required runtime source に昇格する
    v1_に不要な理由: `## OTel Formalization Decision` の通り best-effort observer input のまま。
                     formal adoption は同節の 6 条件を満たしてから
```

snapshot envelope (`## Runtime Observation Snapshot Contract`) は既にこの future scope を
受け止める語彙を予約済みである: `source: otel | sidecar`、`method: poll | imported_event` は
future observer が入ってきたときの分類先として定義されているが、v1 の主経路は
`method: reload | live_query | command_boundary_event` である。

### `mozyo_bridge_pty` inspector / sidecar contract との接続点

receiver-state observability の本命経路は、現行 `mozyo-bridge` 内に detector を生やすことでは
なく、`mozyo_bridge_pty` worktree の sidecar / inspector workstream に接続することである。
local docs では `vibes/docs/logics/ack-completion-receiver-state.md` が canonical な doctrine /
段階分け / 上流 contract への接続を所有しており、本節はその境界をそのまま継ぐ。

```yaml
connection_points:
  canonical_local_doctrine: vibes/docs/logics/ack-completion-receiver-state.md
  upstream_contracts:       # mozyo_bridge_pty worktree (独立 workstream)
    - mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md  # read-only inspector surface
    - mozyo_bridge_pty/vibes/docs/specs/agentd-sidecar-ipc.md                 # sidecar control-event IPC
    - mozyo_bridge_pty/vibes/docs/specs/pty-event-normalization.md            # event 正規化
  staged_bridge:            # ack-completion-receiver-state.md `## Bridge ...` が正本
    - 段階 1: sidecar control event を subscribe し receiver-side signal を inspector に増やす。
              `mozyo-bridge` 側は DeliveryOutcome projection 形を維持する
  envelope_mapping:         # future sidecar signal が来たときの snapshot envelope への対応
    - source: sidecar
    - method: live_query (sidecar 直問い合わせ) | poll | imported_event
    - strength: strong_runtime_signal (workflow truth ではない)
```

sidecar が提供できるのは receiver runtime state / acknowledgement signal であり、owner approval /
review verdict / task completion ではない (`## Responsibility Split` の `### future sidecar /
receiver signal` と同じ境界)。接続段階を進める場合も、`mozyo-bridge` 内に独自 completion
detector を生やさず、`mozyo_bridge_pty` の inspector contract に follow-up task を切る
(`ack-completion-receiver-state.md` の doctrine)。

### 方向制約: receiver-state observability であって completion detection ではない

本 scope の方向は **receiver-state observability の強化** であり、**automatic completion
detection ではない**。

- sidecar / push / polling が増やすのは「receiver runtime が今どうなっているか」の read-only な
  弱い signal であって、「task が完了したか」の判定ではない。
- snapshot / sidecar signal を `completed` / `approved` / `current_status` / `delivered` /
  `accepted` の正本に昇格させない (`### Term restrictions` と同じ境界)。
- `tmux capture-pane` / rendered-text の sentinel 検出を増やして completion を推定する方向には
  倒さない (`ack-completion-receiver-state.md` `## Anti-Patterns`)。pane / stdout silence を
  completion truth にしない境界を sidecar 経路でも守る。
- contradiction / unreadable / stale は `unknown` / `reload_required` を導出し、`healthy` /
  `completed` を導出しない (`### Freshness / fail-safe semantics`)。

### 接続条件 (future scope を v1 へ取り込む gate)

future scope を v1 baseline に昇格させてよい条件。1 つでも欠ければ best-effort enrichment の
ままにとどめ、explicit reload + action-time preflight を必須経路として維持する。

1. missing observer (receiver down / sidecar unavailable / push 受信漏れ) が graceful に
   degrade し、既存の tmux + durable record 挙動へ縮退する。
2. contradiction が `unknown` を導出し `healthy` を導出しない fail-safe が保たれている。
3. observer signal が prompt body / private content を構造的に含まない
   (`### future sidecar / receiver signal` の Failure posture)。
4. OTel formal adoption は `## OTel Formalization Decision` の 6 条件を別途満たす。
5. cockpit UI / consumer は raw observer internals ではなく stable envelope を消費する。

### 非目標 (本 scope で実装しないこと)

- sidecar 実装そのもの (現行 `mozyo-bridge` codebase に sidecar は存在しない。
  `mozyo_bridge_pty` workstream が所有)。
- OTel を workflow truth に昇格すること。
- completion 判定の自動化 (completion detector を作らない)。
- snapshot / sidecar / OTel signal を close / approval / routing の正本にすること。

## Decision

v0.8 では OTel を **formal source of truth** にしない。OTel は experimental /
best-effort observer input のまま扱う。

tmux user options は attention / display の projection cache として使ってよいが、
workflow、routing、owner approval、completion の正本にしない。

future sidecar / `mozyo_bridge_pty` 系の receiver signal は、tmux rendered-text fallback
より強い runtime signal になり得る。ただし、それでも Redmine workflow truth の
代替ではない。sidecar が持てるのは receiver runtime state / acknowledgement signal
であり、owner approval や review verdict ではない。

## Responsibility Split

### Redmine durable state

Owns:

- issue status
- journal gate
- review request / review result
- owner close approval
- implementation_done
- blocked / design consultation record

Does not own:

- pane liveness
- terminal activity
- iTerm / tmux layout
- process health

Failure posture:

- Redmine が読めない場合、workflow state は unknown。
- pane が動いていても Redmine gate が未確認なら owner/review decision は進めない。

### live tmux

Owns:

- pane existence
- session/window/pane current shape
- foreground process / cwd preflight
- target addressability

Does not own:

- owner approval
- review result
- issue completion
- long-term workspace identity
- desired cockpit grouping

Failure posture:

- tmux pane が無いなら target unavailable。
- tmux layout が崩れても workflow state は壊れない。
- live geometry は observed state であり、desired presentation state ではない。

### tmux user option projection

Owns:

- `@mozyo_attention_state`
- `@mozyo_attention_severity`
- `@mozyo_attention_reason`
- `@mozyo_attention_updated_at`
- other short-lived display hints

Does not own:

- derived attention truth
- routing safety
- durable workflow state
- completion / acknowledgement

Failure posture:

- user option が無い、古い、手で改変された場合は projection stale。
- 再計算できるので、消失しても workflow は壊れない。

### OTel event store

Owns:

- best-effort runtime event history
- activity / idle / unknown input
- event timeline runtime layer
- per-CLI event depth investigation

Does not own:

- liveness death判定
- owner/review state
- completion acknowledgement
- durable workflow record

Failure posture:

- receiver down: events lost by design。
- store missing: empty / unknown。
- unobserved agent: activity unknown, tmux livenessへ縮退。
- OTel silence: idle or unknown, not dead。

Current adoption judgment:

- Keep experimental.
- Keep doctor warning / guidance.
- Do not make receiver required for ordinary `mozyo` / handoff / target discovery.
- Use it as enrichment only when present.

Formal adoption conditions:

1. receiver startup / restart runbook is stable;
2. managed pane launch reliably injects safe resource attributes;
3. event depth is sufficient for the claimed UI use case;
4. privacy / prompt non-capture tests remain pinned;
5. missing receiver behavior is still graceful;
6. cockpit UI consumes a stable envelope, not raw OTel internals.

### future sidecar / receiver signal

Owns, if implemented:

- receiver prompt / queue / acknowledgement state
- machine-readable receiver pickup / assistant-turn boundary signal
- terminal runtime state that tmux rendered text cannot reliably express

Does not own:

- Redmine gate state
- owner approval
- review verdict
- project management truth

Failure posture:

- sidecar unavailable must degrade to existing tmux + durable record behavior.
- sidecar contradiction must derive unknown, not healthy.
- sidecar data must avoid prompt body / private content by construction.

## Attention Derivation Boundary

attention state is derived. It is not stored as a mutable truth field.

Inputs:

- Redmine workflow state
- live tmux liveness / target preflight
- OTel or sidecar activity signal when available
- managed event / desired presentation state when available

Output:

- AttentionRecord
- text / JSON columns
- tmux user option projection
- UI color / badge / title

Rules:

- owner_waiting beats activity.
- blocked beats stalled.
- review_waiting beats stalled.
- unreadable or contradictory input becomes unknown.
- healthy requires readable sources and no stronger signal.

## tmux-ui Host Wiring

`tmux-ui host wiring` should remain operator opt-in for now.

理由:

- It changes host tmux config.
- It is presentation, not workflow truth.
- Current doctor can explain the missing wiring and provide a command.
- Making it mandatory would couple all users to cockpit presentation defaults.

However, guidance should be stronger in contexts that explicitly request cockpit attention
projection or tmux-native UI.

Recommended posture:

- ordinary `doctor`: warning / next_action, not blocker.
- `doctor --profile cockpit-ui` or future cockpit-specific doctor: stronger warning.
- release gate: check artifact and scaffold, but do not require host wiring to be installed on
  the maintainer machine.

## OTel Formalization Decision

Do not graduate OTel to stable required runtime state in v0.8. Treat it as:

- supported experimental observer input;
- optional enrichment for event timeline / activity;
- non-authoritative input to attention derivation;
- a candidate for stronger adoption after sidecar / managed launch / privacy proof is stable.

This is not a rejection of OTel. It is a refusal to make a best-effort push store the truth
source for stopped / owner-waiting / completion state.

## UI / Color Boundary

Color and UI state are never source of truth.

Allowed:

- derive attention record;
- project to tmux user options;
- render labels or colors from those options;
- let private UI consume `agents targets`, `events`, and attention records.

Forbidden:

- use color as owner approval;
- use pane title as role identity;
- use WebViewer DOM state as review completion;
- use iTerm split geometry as workspace/lane truth;
- write private color policy into OSS defaults.

## Follow-up Split

- If OTel adoption is strengthened, create a task for receiver/runbook/privacy/event-depth
  proof before making it required.
- If tmux-ui guidance is strengthened, create a cockpit-specific doctor task rather than
  making generic doctor fail.
- If sidecar is pursued, define a receiver-state contract before wiring it into attention.
- If UI needs more fields, add public-safe projection fields first; keep private UI policy
  outside mozyo-bridge.
- Runtime observation snapshot contract (#12223) の follow-up: reload command (#12224)、
  cockpit UI reload / stale display (#12225)、action-time live preflight (#12226)、future
  sidecar scope (#12227)。各 issue への引き継ぎ条件は `## Runtime Observation Snapshot
  Contract` の `### Contract handoff to follow-up issues` を正本とする。
- Future push / sidecar observer scope split (#12227) の正本は `## Future Push / Sidecar Observer
  Scope Split`。v1 = explicit reload + action-time preflight、polling / push / sidecar / OTel
  formal adoption = future scope、接続点は `ack-completion-receiver-state.md` 経由の
  `mozyo_bridge_pty` inspector / sidecar contract。方向は receiver-state observability であって
  automatic completion detection ではない。
