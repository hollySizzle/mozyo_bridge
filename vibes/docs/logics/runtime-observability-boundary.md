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
```

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
- machine-readable completion or pickup signal
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
