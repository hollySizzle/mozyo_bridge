# Cockpit Attention State Projection

Redmine #11935 / #11950。cockpit / sublane 運用で pane が増えた時に、
owner 判断待ち、review 待ち、blocked、stalled、done を目視しやすくするための
attention state 設計正本。

本 doc の結論は単純である。**色や pane 表示を正本にしない**。attention state は
Redmine journal / issue status / managed event / unit desired state / live runtime
observation から導出する。tmux / cockpit / iTerm の色は、その導出結果の
projection にすぎない。

## なぜ必要か

cockpit dogfooding では、main coordinator lane、sublane Codex、sublane Claude が
同じ tmux cockpit group に並ぶ。pane 数が増えると、次の状態が見えづらくなる。

- owner close approval 待ちで止まっている。
- review_request が main coordinator に戻っているが未処理。
- sublane が callback を返さず停止している。
- implementation は終わったが close / retirement が未処理。
- blocked なのか、単に長い command 実行中なのかが pane 目視だけでは分からない。

この問題を iTerm profile や手動色変更だけで解くと、表示と durable state がずれた
時に workflow 判断を誤る。したがって UI 表示は derived projection に限定する。

## 正本境界

```text
workspace identity      -> registry.sqlite + workspace anchor
lane / unit desired     -> managed events + future unit/presentation current table
workflow state          -> Redmine issue status / journal gate
runtime liveness        -> live tmux observation
activity signal         -> OTel / event store / target observation cache
attention_state         -> derived value
tmux / cockpit / color  -> projection only
```

attention state はどこか 1 つの mutable color field に保存しない。必要なら future
`unit_desired_state` / presentation current table に **latest derived value cache** を
置いてよいが、その cache は再計算可能でなければならない。

## Derived State Contract

attention state は `AttentionRecord` のような projection record として扱う。

```yaml
attention_record:
  unit_id: string
  host_id: string
  workspace_id: string
  lane_id: string
  role: codex | claude | other
  target_key: string | null
  attention_state: healthy | owner_waiting | review_waiting | blocked | stalled | done | retired_candidate | unknown
  severity: normal | notice | warning | critical
  reason_code: string
  source_refs:
    - redmine:#11935#journal
    - managed_event:<id>
    - tmux:<pane_id>@<observed_at>
  observed_at: ISO8601
  expires_at: ISO8601 | null
```

`source_refs` は人間が追える anchor であり、secret や private path を入れない。
Redmine URL / journal id / pane id / event id 程度に抑える。

## MVP State Set

初期 state は増やしすぎない。色や label の見栄えではなく、operator action を
分岐できる最小集合にする。

### healthy

通常状態。特別な owner / review / blocked signal がなく、runtime observation も
最近である。

表示は弱くする。健康な pane を派手にすると、本当に見るべき pane が埋もれる。

### owner_waiting

owner の明示判断が必要な状態。

導出元の例:

- owner close approval gate が未記録。
- production publish / destructive operation / credential / guardrail など、
  standing delegation では進められない carve-out がある。
- Redmine journal に owner approval requested が記録されている。

operator action: main coordinator が owner に確認する。

### review_waiting

implementation_done / review_request が durable record に存在し、Codex review が
未完了の状態。

導出元の例:

- `review_request` kind の handoff / journal がある。
- 対象 issue が review pending 相当の status / comment を持つ。
- sublane callback が main coordinator 宛てに記録済み。

operator action: coordinator Codex が Redmine anchor を読んで review する。

### blocked

明示的に blocked と記録された状態。単なる無活動とは分ける。

導出元の例:

- Redmine journal に blocker / blocked reason がある。
- command が fail-closed し、next action が owner / external state に依存する。
- gate 条件が未充足で実装者が停止している。

operator action: blocker の解除、scope 変更、owner 判断、または ticket 分割。

### stalled

期待された callback / progress が一定時間ない状態。これは推定であり、blocked より
弱い。

導出元の例:

- lane 作成後、start / implementation_done / review_request が一定時間ない。
- Claude pane 起動後、auto permission mode 待ちなどで user input に見える状態が続く。
- sublane Codex が main coordinator に callback しない。

operator action: coordinator が target lane を inspect し、必要なら ping / restart /
ticket 追記を行う。stalled は自動で close / reroute しない。

### done

作業は完了し、close gate も満たしている状態。まだ pane は残っていてもよい。

operator action: lane retirement candidate として見る。

### retired_candidate

lane / worktree / pane を片付けてよい候補。done より operational な projection。

導出元の例:

- issue closed。
- branch merged / pushed。
- no dirty worktree。
- owner / coordinator が lane retire を許容している。

operator action: cockpit layout から退役させる、または worktree cleanup task へ。

### unknown

必要な source が読めない、または矛盾している状態。healthy に倒さない。

operator action: source-of-truth を読みに行く。表示が壊れた場合も workflow 判断は
Redmine / runtime preflight に戻す。

## Projection Strategy

tmux-native を primary projection にする。iTerm 固有色は operator-local optional
projection とし、OSS default の正本にしない。

### tmux user option

pane / window に user option を付ける。

```text
@mozyo_attention_state=review_waiting
@mozyo_attention_severity=notice
@mozyo_attention_reason=review_request_pending
@mozyo_attention_updated_at=2026-06-15T00:00:00Z
```

利点:

- tmux-native。
- `agent-ui.conf` から参照できる。
- projection を消しても durable state は残る。

注意:

- user option は cache/projection。手で書き換えられても正本ではない。
- handoff preflight / routing 判定に使わない。

### status / title projection

`agent-ui.conf` や cockpit rendering が user option を読んで、pane border / window
status / pane title に小さく出す。

初期案:

```text
healthy           -> no marker
owner_waiting     -> OWNER
review_waiting    -> REVIEW
blocked           -> BLOCKED
stalled           -> STALLED
done              -> DONE
retired_candidate -> RETIRE
unknown           -> UNKNOWN
```

色は label の補助にする。色だけに依存しない。

### command output projection

`agents targets` / future `cockpit status` は attention columns を出してよい。

```text
WORKSPACE  LANE  ROLE   ATTENTION       REASON
mozyo      11935 codex  review_waiting  review_request_pending
```

text / JSON の両方で出せるようにする場合も、attention は derived record として
出す。routing key ではない。

## Derivation Priority

矛盾時は安全側に倒す。

```yaml
priority:
  1_owner_waiting:
    stronger_than: [review_waiting, stalled, healthy]
    reason: owner 判断が必要な gate を埋もれさせない
  2_blocked:
    stronger_than: [stalled, healthy]
    reason: 明示 blocker は無活動推定より強い
  3_review_waiting:
    stronger_than: [stalled, healthy]
    reason: coordinator action が明確
  4_stalled:
    stronger_than: [healthy]
    reason: 推定なので明示 gate より弱い
  5_done_or_retired_candidate:
    condition: close gate + clean/merged/pushed など operational 条件
  6_healthy:
    condition: above none + source readable
  unknown:
    condition: source unreadable or contradictory
```

## Redmine / Runtime の扱い

Redmine は workflow state の正本だが、tmux liveness の正本ではない。tmux は
runtime liveness の正本だが、owner approval / review state の正本ではない。

attention derivation は両方を読むが、どちらかへ責務を寄せすぎない。

- Redmine が `review_request` でも pane が死んでいるなら `review_waiting` +
  reason `target_dead` のように表現する。
- pane が動いていても owner approval が必要なら `owner_waiting` を優先する。
- pane が見えなくても issue closed / branch merged なら `done` または
  `retired_candidate` にできる。

## Public / Private Boundary

OSS default は generic な state 名と projection hook だけにする。

入れてよいもの:

- state enum
- reason_code の generic set
- tmux user option 名
- JSON/text output
- `agent-ui.conf` の控えめな default label

入れてはいけないもの:

- private project の lane naming policy
- operator 固有の配色
- iTerm profile 変更
- 社内用の escalation policy
- private Redmine project 名や個人名

## Implementation Split

本 task は design doc だけで完了する。実装は分割する。

推奨 task:

1. `AttentionRecord` derivation read model を追加する。
2. `agents targets` / future `cockpit status` に attention projection を出す。
3. tmux user option projection writer を追加する。
4. `agent-ui.conf` で attention label / color を控えめに表示する。
5. stalled / callback-missing の derivation を Redmine / managed event から pin する。

各 task は runtime / tests を伴うため Claude implementer lane に回し、Codex は review
する。特に tmux projection は表示変更であって routing / handoff safety gate を触らない
ことを test で固定する。

## Verification Notes

実装時に最低限 pin すること:

- attention state を handoff target resolver の routing decision に使っていない。
- tmux user option を消しても Redmine / runtime / managed event から再導出できる。
- `owner_waiting` と `review_waiting` が `stalled` より優先される。
- source unreadable は `healthy` ではなく `unknown`。
- iTerm 固有設定が OSS default に入っていない。
