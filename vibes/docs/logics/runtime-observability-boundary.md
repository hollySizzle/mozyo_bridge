# Runtime Observability Boundary

Redmine #12003。runtime observability を v0.8 の境界として整理し、Redmine durable
state、live tmux、tmux user option projection、OTel event store、future sidecar の責務を
分ける設計正本。

この文書は docs-only の判断材料であり、OTel / sidecar / UI の実装採用を確約しない。

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
