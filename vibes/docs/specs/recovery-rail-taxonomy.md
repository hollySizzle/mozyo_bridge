# 回復レール taxonomy と統合 spec (製本 Phase 1)

- status: active
- 対象 Redmine: #15841「回復レール群の taxonomy と統合 spec を作る (製本 Phase 1)」
- 親: #15631「3階層構造を実運用可能にする」/ ADR-0011 (責務分担モデル)
- 読取 base: `origin/main@289343dbe5df14472ecb35906dffcde67a4121ab`

## 本 doc の位置づけ

`sublane` サブコマンド群のうち「壊れた lane / pair を回収する」レールは、個別 Redmine
issue ごとに 1 本ずつ足されてきた。各レールは単体では fail-closed で正しいが、**集合として
設計された記録が無い**ため、次の 2 つが起きている。

- ある pair shape をどのレールも subject にしていない (gap)。#15811 が埋めた
  `declared_pins_absent` class はその実例。
- 同じ shape を複数レールが別語彙で扱う (overlap)。

本 doc はその集合を 1 か所に固定する。**本 doc は分析と提案であり、統合の実施ではない**
(#15841 受け入れ条件 2: コード・テストの挙動変更なし)。どのレールを統合 / deprecate するか
の判断は coordinator / owner の escalation 対象であり、本 doc は判断しない。

**用語 (proposed term)**: 本 doc で「**pair shape**」とは、1 lane の回収可否を決める
durable + live の観測軸ベクトル (`## 2. pair shape の軸`) を指す。既存の `lane shape` /
`lane state` とは別語であり、runtime token には未配線の記述用語である。

正本の分担: lifecycle 状態遷移そのものの正本は `vibes/docs/logics/managed-state-model.md`、
identity / pin 解決の正本は `vibes/docs/specs/herdr-native-identity.md` と
`vibes/docs/specs/route-identity-ledger.md`。本 doc はそれらを再宣言せず、**回収レールの
選択規則**だけを持つ。

## 1. taxonomy

`f_140_delegated_coordinator_nested_handoff` に登録された `sublane` サブコマンドのうち、
回収 / 終端に関与する 25 本。「発生源」は各 module docstring が名指す実測事象。

### 1.A 診断レール (read-only、状態を書かない)

| レール | 何をするか | 前提 pair shape | 発生源 | 出力 disposition | 主な typed refusal |
| --- | --- | --- | --- | --- | --- |
| `reboot-audit` | Redmine / git / lifecycle row / live inventory の 4 authority を **1 snapshot** で join し、lane ごとの次レールを typed で返す | 任意 (全 lane 走査) | host reboot (#14499、実測 #13490 j#89060: 23 pane 中 15 が shell residue) | `restore_worktree` / `terminalize_bound_metadata` / `terminalize_unbound_metadata` / `close_shell_residue` / `guarded_close` / `resume` / `hibernate` / `supersede` / `already_terminal` / `unknown` / `blocked` | `issue_state_unreadable` / `inventory_unreadable` / `worktree_presence_unknown` / `head_not_integrated` / `foreign_occupant` |
| `rehydrate-fleet` (plan) | manifest が active と呼ぶ lane 群に対し「未配送の action」を per-lane で決める。plan の effect budget は 0 | ACTIVE row + OPEN issue | host restart で全 pane の attestation 失効。`herdr session-start` は default pair しか戻さない (#15745) | `heal_pair` / `restore_dispatch` / `resume_brief`、または typed skip / block | `dispatch_uncertain` / `dispatch_record_unreadable` / `startup_interaction_required` / `foreign_slot` / `lane_moved` |
| `quarantine-inspect` | 1 role の assigned name / locator / revision / attested generation / quarantine action id / generation mismatch 軸を報告し、貼付可能な owner approval を render | 任意の managed receiver | approval に必要な 5 token が公開 read 面から取得できず #14163 の 6-lane drain が停止 (#14234) | `ready` + approval template、または typed refusal | `workspace_unresolved` / `inventory_unreadable` / `composer_unreadable` / `duplicate_receiver` / `not_quarantine_candidate` |
| `callback-recovery` | delivered-but-quiet な作業単位を durable-record の事実から 4 つの callback-stall class に分類し、標準回収経路を出力 | 任意 | callback が送られたのに durable gate が落ちない (#12159 / #13520) | 4 class + 回収経路。genuine stall で非 0 exit | — (read-only、権限変更なし) |
| `recover-restored-pair` | reboot 復元で cwd / startup identity proof が不整合になった ACTIVE lane の exact idle 世代を検査 | ACTIVE + pin あり + 両 slot live + cwd drift または attestation non-green | reboot restoration (#15227) | **preflight のみ** (下記 GAP-1 参照) | `managed_slot_busy` / `managed_pair_already_healthy` / `pending_composer_loss_not_approved` / `generation_conditional_close_unavailable` |
| `list` | pane inventory から live sublane を列挙し stale/retire hint を出す | 任意 | 日常運用 | advisory hint のみ | — |

### 1.B metadata レール (lifecycle row のみ書く。process を触らない)

| レール | 何をするか | 前提 pair shape | 発生源 | 出力 disposition | 主な typed refusal |
| --- | --- | --- | --- | --- | --- |
| `adopt-restored-pair` | pin snapshot が **exactly absent** な ACTIVE row に、live 復元 pair から **初回** pin を宣言 (empty-only CAS `backfill_active_binding`) + generation / participant / attestation を re-attest | ACTIVE + `declared_pins_absent` + 両 slot が server-restored | herdr server generation 変更 (#15795) 後、create path が空 pin で宣言した row のまま復元された (#15811) | pin 宣言 + re-attest lineage。`lane_generation` 不変 | `declared_pins_present:<pin_pair_reason>` / `generation_absent:<slot>` / `ambiguous_live_locators` / `lane_not_active` |
| `rebind-restored-pair` | pin snapshot が **stale な exact old pair** の ACTIVE row を、新 locator へ replace-CAS。#15769 で generation row / participant locator の re-attest も追加 | ACTIVE + pin 解決可 + locator drift あり | herdr server restart が同一 session を新 pane へ復元 (#15656 / #15769) | pin 置換 + old→new lineage | `declared_slots_unresolved` / `locator_not_drifted` / `declared_locator_still_live` / `unattested_slot:<slot>` / `terminal_unchanged_noop:<slot>` |
| `repair-pins` | hibernated / released / **bound** row の **空** pin snapshot を、live idle attested pair から repair。`recover-pair` の preflight を通す準備 | hibernated + released + `worktree_identity` あり + pins **empty** + pair live | #13846 j#79915 で `recover-pair` が `hibernated_record_missing_pins` で永久 fail | `repaired` / `already_repaired` (byte-equal のみ) | `declared_pins_divergent` / `live_pair_absent` / `not_repairable_state` / `revision_race` / `generation_race` |
| `repair-worktree-binding` | hibernated / released row の **空** `worktree_identity` を、実在 checkout の positive evidence から 1 field だけ書く | hibernated + released + pin あり + `worktree_identity` **empty** | #13809 の backfill が active-row 限定で、hibernated row では `unexpected_state` zero-write (#14475) | `repaired` | `worktree_not_a_live_checkout` / `worktree_path_is_not_the_worktree_root` / `worktree_belongs_to_a_foreign_workspace` / `worktree_branch_is_not_the_lane_branch` / `hibernated_record_missing_pins` |
| `reconcile-recovered-pair-pins` | `recover-pair` で回収済みの exact active pair について、**stale な宣言 pair snapshot だけ**を置換 (旧 2 件 exact 必須の置換専用 CAS) | ACTIVE + 回収済 pair + 旧 pin 2 件 exact | #14203 R19 | pin 置換 | authority field 不一致による zero-write |

### 1.C process 置換レール (close / launch を伴う)

| レール | 何をするか | 前提 pair shape | 発生源 | 出力 disposition | 主な typed refusal |
| --- | --- | --- | --- | --- | --- |
| `recover-stale` | lane の **worker 1 本**が turn 後に消えた状態を、close → launch → attest → redispatch で回収。gateway / coordinator / foreign は保護 | ACTIVE + worker が positively stale (shell residue signal) | worker process が turn 後に消え、Implementation Done / Review Request diff が durable 化されない (#13806) | `actionable` → close+relaunch+redispatch | `productive_provider_or_tool_child` / `gateway_or_foreign_protected` / `not_stale` / `dirty_state_unreadable` / `authority_conflict` / `stale_generation` |
| `recover-gateway` | lane の **gateway 1 本**が confirmed delivery 直後に turn を終え durable gate を落とさない状態を refresh。worker / default coordinator / foreign は保護 | ACTIVE + gateway が live settled `turn_ended` + turn 分類が `turn_failed_no_durable_gate` | #14203 dogfood: 5 lane が `sent`/`started` のまま durable 応答 0 | close+relaunch+attest → `callback_recovery_once` で既存 anchor を再配送 | `non_gateway_protected` / `turn_not_classified_failed` / `pending_composer_input` / `no_resume_anchor` / `worker_not_distinguished` |
| `refresh-worker` | **live なのに非生産的**な worker (confirmed resume 後に `turn_ended` で durable progress 0、dirty worktree 保持) を refresh | ACTIVE + worker が **live** + turn 分類 failed + worktree dirty 可 | #14658 lane j#92366 の実測 | close+relaunch+attest → resume 1 回 | `worker_not_settled` / `gateway_not_distinguished` / `dirty_state_unreadable` / approval の strict marker 不一致 |
| `recover-pair` | **hibernated** lane の fresh launch が部分 boot (unattested / stale) した pair を、**bad generation だけ** close → relaunch → resume → redispatch | hibernated + pin 解決可 + `resume` が `pair_not_attested` を返す状態 | #13847 (hibernate 後の再起動が半端に成功) | slot ごとに `recover_bad_generation` / `healthy_no_action` / 5 種の `preserve_*` | `preserve_productive` / `preserve_pending_composer` / `preserve_foreign` / `preserve_ambiguous` / `preserve_newer_generation` |
| `converge-bound-pair` | hibernated / released / bound で **pins empty** の lane の stale/unattested pair を置換し、fresh attested pair から pin を repair | hibernated + released + bound + pins **empty** | #13933 | `actionable` / `already_converged` / `blocked` | `not_hibernated_released_bound_pins_empty` / `pair_contains_preserved_slot` / `fresh_pair_unproven` / `pin_cas_refused` |
| `prepare-bound-pair` | 上と同じ signature の pair のうち、**uncorrelated な pending composer 世代だけ**を owner 承認付きで discard し relaunch | 同上 + 特定 role に pending composer | convergence が pending composer を必ず保存するため前段が要る (#13933) | `prepared` | `no_exact_uncorrelated_pending_composer` / `pair_contains_non_discardable_preserved_slot` / `approval_mismatch` |
| `quarantine` | 1 receiver の pending composer を分類し、generation-bound owner 承認のもとで **generic Enter/C-u/body typing 無しに** receiver を置換 | 任意 lane + receiver が pending composer 保持 | #13763 / #15193 (generation mismatch × real pending input) | receiver 置換 | 5 つの `--approved-*` token 不一致 / `not_quarantine_candidate` |
| `close-residue` | 当該 lane 自身の **shell residue pane だけ**を close (assigned name byte 一致 + stale 分類 + 活動 0)。live half が 1 つでもあれば plan 全体が 0 に潰れる | 任意 disposition + residue pane あり | reboot 後、名前だけ残った pane が unit を占有し terminal retire を永久に塞ぐ (#14499 / #13518) | `closed` / `no_residue` | `live_pair_present` / `residue_identity_moved` / `residue_generation_unverified` / `lane_owner_unverified` / `launch_in_flight` |

### 1.D 配送レール (send のみ。process も row も触らない)

| レール | 何をするか | 前提 pair shape | 発生源 | 出力 disposition | 主な typed refusal |
| --- | --- | --- | --- | --- | --- |
| `recover-pair-delivery` | 既に active な回収済 pair へ、**元の** implementation_request を 1 つの新 recovery action として配送。先行 fence row は保持し解放しない | ACTIVE + 回収済 pair | #14203 R17 | `implementation_request_redelivered`、または `redispatch_fate_unresolved` | `already_redispatched` / `redispatch_target_retiring` / `redispatch_send_failed` / `redispatch_uncertain` |
| `recover-worker-delivery` | 同じ IR を、gateway を経由せず **worker へ直接**配送。別個の strict owner-approved action | ACTIVE + 回収済 pair | #14203 R18 | 同上 | 同上 |
| `rehydrate-fleet --execute` | plan が名指した `restore_dispatch` / `resume_brief` を、既存 handoff rail を composeして実行 | 上記 1.A の plan | #15745 | 配送 | `dispatch_uncertain` は **block であって retry ではない** |

### 1.E lifecycle disposition レール (CAS のみ)

| レール | 何をするか | 前提 pair shape | 発生源 | 出力 disposition | 主な typed refusal |
| --- | --- | --- | --- | --- | --- |
| `hibernate` | **open issue** の lane の managed process を、worktree / branch / 未 push commit / metadata / callback route を保存したまま release | ACTIVE + 積極的 park basis (dependency park または early hibernate) | 依存待ちで pane を占有し続ける lane (#13682、実測 #13441) | `active → hibernated` + process release | park basis 不成立 / callback 未 drain / review 未了 / pending composer / 未 push commit (early hibernate 時) |
| `resume` | hibernated lane を、fresh pair の検証後に active へ戻す。**launch はしない、verify + flip のみ** | hibernated + release settled + fresh pair が両 slot live + generation fence 通過 | #13682 / #14756 | `hibernated → active` | `lane_not_hibernated` / `release_generation_in_flight` / `issue_reowned_by_another_lane` / `pair_not_both_slots_live` / `pair_not_attested` / `fresh_pair_pins_unresolved` |
| `supersede` | issue の所有権を後継 lane へ引き渡し、旧 lane の process を release (tombstone-free) | 両 lane の identity 既知 + 後継が attested + 同一 issue + 旧 lane idle | #13681 | ownership 移管 + release | 両 identity / attestation / idle 不成立 |
| `retire` (既定) | git probe + durable invariant から retire 判定。`--execute` は guarded close | issue closed + 各 invariant | #13754 | `retired` / `blocked` / `deferred` / `uncertain` / `already_retired` | `zero_close_unproven` / `worktree_binding_unverified` / `retire_identity_changed` |
| `retire --migrate-hibernated-legacy` | hibernated + released + **worktree binding 空** の row を metadata のみで terminal 化 | hibernated + released + binding **empty** + live 0 | #13841 (実測 #13756 j#79114) | `retired` | `head_not_integrated` / `live_pair_present` / `foreign_inventory_present` |
| `retire --reconcile-hibernated-live` | hibernated + binding 空だが **exact pair が live** な矛盾を、1 revision CAS + 専用 owed-close pin で解消 | hibernated + binding empty + pair **live** | #13842 / #15227 | `retired` | 部分 pair / 重複名 / stale residue / unattested / busy / pending composer |
| `retire --retire-hibernated-bound` | hibernated + released + **bound** row を metadata のみで terminal 化 | hibernated + released + bound + live 0 | #13845 (実測 #13810 j#79416) | `retired` | `worktree_binding_mismatch` / `bound_retire_worktree_branch_mismatch` |
| `retire --retire-active-live-zero` | **ACTIVE bound** row で pair が既に positively 0 の lane を terminal 化 | active + bound + live 0 + issue closed + head integrated | #14242 (実測 #14222 j#85208) | `retired` | 不読 inventory / duplicate slot / foreign occupant / revision race |
| `retire --retire-active-unbound-live-zero` | **ACTIVE + binding 空** + live 0 を terminal 化。worktree attestation の代わりに `(lane_generation, revision)` 宣言 CAS | active + binding empty + live 0 | #14499 (実測 #14456 j#87973) | `retired` | 同上 + generation/revision 不一致 |
| `retire --retire-hibernated-unbound-live-zero` | hibernated + released + binding 空 + live 0 を terminal 化 | 同左 | #14716 | `retired` | 同上 |
| `audit-failure-terminal record` | 独立監査失敗 lane の terminal retire 判断を staging | ― | #15166 | **retire を一切 authorize しない診断記録** | `coordinator_receipt_authority_unresolvable` (#15195 待ち) |

## 2. pair shape の軸

決定木の入力ベクトル。**すべての軸は「読めなかった = unknown」を値と区別する** (`unknown`
は必ず block へ落ちる。`reboot_residue_convergence` の "Unknown is not absence"、
`fleet_rehydrate` の "An axis that could not be read is None and yields a block")。

```yaml
pair_shape:
  disposition:        [active, hibernated, retired, superseded, unknown]
  worktree_identity:  [bound, empty, unknown]
  declared_pins:      [resolvable, absent, degraded, unknown]   # degraded = unreadable|foreign_pin_role|mixed_pin_role_vocabulary|duplicate_pin_role|incomplete_pin_pair
  process_release:    [not_requested, in_flight, released, unknown]
  live_pair:          [both_live, half_live, zero_live_positive, shell_residue, foreign_occupant, unknown]
  slot_health:        [attested_cwd_ok, unattested, stale_generation, cwd_drifted, locator_drifted, turn_ended_unproductive, busy, pending_composer, unknown]
  issue_state:        [open, closed, unknown]
  head_integrated:    [true, false, unknown]
```

`declared_pins` の値語彙は `core/state/lane_pin_role.py` の `PIN_PAIR_*` 正本
(`PIN_PAIR_OK` / `PIN_PAIR_ABSENT` / `PIN_PAIR_UNREADABLE` / `PIN_PAIR_FOREIGN` /
`PIN_PAIR_MIXED` / `PIN_PAIR_DUPLICATE` / `PIN_PAIR_INCOMPLETE`) をそのまま折り畳んだもの。

## 3. 決定木

順序付き規則。**上から最初に match した rule を採る**。すべての rule は「その shape を
subject と宣言しているレールが実在するか」で書かれており、match しない shape は
`GAP-*` に落ちる。

```yaml
# 前提: どの軸でも unknown があれば R0 が先に発火する。
rules:
  - id: R0
    when: {any_axis: unknown}
    rail: reboot-audit          # または rehydrate-fleet plan
    note: "読めない軸がある間は回収レールを選ばない。診断が先。"

  - id: R1
    when: {live_pair: shell_residue}
    rail: close-residue
    note: "residue を閉じるまで live-zero 読取は正直にならない。terminal 化は別段。"

  - id: R2
    when: {disposition: active, declared_pins: absent, live_pair: both_live, slot_health: [locator_drifted, unattested]}
    rail: adopt-restored-pair   # #15811 が埋めた class
    note: "create path の正常 shape。pin 不在は record 劣化ではない。"

  - id: R3
    when: {disposition: active, declared_pins: resolvable, slot_health: locator_drifted}
    rail: rebind-restored-pair

  - id: R4
    when: {disposition: active, declared_pins: resolvable, slot_health: cwd_drifted}
    rail: recover-restored-pair
    status: diagnose_only        # GAP-1: 実行経路が存在しない

  - id: R5
    when: {disposition: active, slot_health: turn_ended_unproductive, slot: gateway}
    rail: recover-gateway

  - id: R6
    when: {disposition: active, slot_health: turn_ended_unproductive, slot: worker, live: true}
    rail: refresh-worker

  - id: R7
    when: {disposition: active, slot: worker, live: false, stale_signal: positive}
    rail: recover-stale

  - id: R8
    when: {disposition: active, slot_health: pending_composer}
    rail: quarantine
    requires: quarantine-inspect  # approval token の取得元

  - id: R9
    when: {disposition: hibernated, worktree_identity: bound, declared_pins: absent, process_release: released, live_pair: both_live}
    rail: [repair-pins, converge-bound-pair]
    note: "OVERLAP-2。actuation 予算が違うだけで subject signature は同一。"

  - id: R10
    when: {disposition: hibernated, worktree_identity: bound, declared_pins: absent, slot_health: pending_composer}
    rail: prepare-bound-pair
    then: converge-bound-pair

  - id: R11
    when: {disposition: hibernated, worktree_identity: empty, declared_pins: resolvable, process_release: released}
    rail: repair-worktree-binding

  - id: R12
    when: {disposition: hibernated, declared_pins: resolvable, slot_health: [unattested, stale_generation]}
    rail: recover-pair

  - id: R13
    when: {disposition: hibernated, live_pair: both_live, issue_state: open, all_gates: green}
    rail: resume

  - id: R14
    when: {disposition: active, issue_state: open, dispatch: owed}
    rail: [recover-pair-delivery, recover-worker-delivery, rehydrate-fleet]
    note: "OVERLAP-4。3 経路が同じ『元の IR を再配送』を提供する。"

  - id: R15
    when: {issue_state: closed, head_integrated: true}
    rail: retire
    intent_by:
      - when: {disposition: hibernated, worktree_identity: bound, live_pair: zero_live_positive}
        intent: ["--retire-hibernated-bound"]
      - when: {disposition: hibernated, worktree_identity: empty, live_pair: zero_live_positive}
        intent: ["--migrate-hibernated-legacy", "--retire-hibernated-unbound-live-zero"]
      - when: {disposition: hibernated, worktree_identity: empty, live_pair: both_live}
        intent: ["--reconcile-hibernated-live"]
      - when: {disposition: active, worktree_identity: bound, live_pair: zero_live_positive}
        intent: ["--retire-active-live-zero"]
      - when: {disposition: active, worktree_identity: empty, live_pair: zero_live_positive}
        intent: ["--retire-active-unbound-live-zero"]
      - when: {disposition: active, live_pair: both_live}
        intent: []          # 既定 retire --execute (guarded close)
    note: "OVERLAP-3。6 intent + 2 migration が 1 表に折り畳める。"

  - id: R16
    when: {issue_state: open, park_basis: affirmative}
    rail: hibernate

  - id: R17
    when: {successor_lane: attested, same_issue: true, original: idle}
    rail: supersede

  # --- fall-through: ここへ落ちる shape は subject を持つレールが無い ---
  - id: R98
    when: {declared_pins: degraded}
    rail: none
    gap: GAP-2

  - id: R99
    when: {disposition: hibernated, worktree_identity: empty, declared_pins: absent}
    rail: none
    gap: GAP-3
    note: "repair-worktree-binding は pin を要求し、repair-pins / converge-bound-pair は bound を要求する相互 deadlock。terminal 化のみ到達可能。"
```

### 3.1 gap (どのレールも subject にしていない shape)

| id | shape | 現状 | 根拠 |
| --- | --- | --- | --- |
| **GAP-1** | `active` + pin 解決可 + `cwd_drifted` / attestation non-green | `recover-restored-pair` が唯一の subject だが、`RestoredPairPlan.generation_conditional_close_available` が**常に `False` を返す固定 property** であり、`blocked_reasons` に `generation_conditional_close_unavailable` が必ず入る。したがって `may_recover` は**構造的に常に False**。診断は在るが**回収経路は存在しない** | `domain/restored_pair_recovery.py` L163-234。CLI help も "Read-only until Herdr exposes an atomic generation-conditional close primitive" と明記。Herdr 0.8 / protocol 19 が close 変異に `pane_id` しか受けないことが根本原因。**実測 (2026-08-21, base `289343db`)**: 全 identity 充足 / `lifecycle_current=True` / `worktree_authority_current=True` / `allow_pending_composer_loss=True` / 片 slot だけ `cwd_matches=False` という最良 shape を構成しても `blocked_reasons == ('generation_conditional_close_unavailable',)` / `may_recover=False` |
| **GAP-2** | 任意 disposition + `declared_pins: degraded` (`unreadable` / `foreign_pin_role` / `mixed_pin_role_vocabulary` / `duplicate_pin_role` / `incomplete_pin_pair`) | **どのレールも subject にしていない。** `adopt-restored-pair` は `declared_pins_present:<reason>` で明示拒否 (劣化 snapshot を上書きすると証拠が消えるため、これは意図的な正しい拒否)。`rebind-restored-pair` は exact 旧 pair 2 件を要求し `declared_slots_unresolved`。`repair-pins` は **empty** 限定。`repair-worktree-binding` は `declared_pins_fail_validation` / `declared_pins_are_not_canonically_encoded` で拒否。`read_declared_pin_pair` の全 consumer が非 OK を refusal として扱い、subject として消費する箇所は 0 | `src/` 全体で `PIN_PAIR_FOREIGN` / `_MIXED` / `_DUPLICATE` / `_INCOMPLETE` / `_UNREADABLE` を参照するのは定義元 `lane_pin_role.py` のみ (grep で確認) |
| **GAP-3** | `hibernated` + `released` + `worktree_identity: empty` + `declared_pins: absent` | **相互 precondition の deadlock。** `repair-worktree-binding` は pin を要求し `hibernated_record_missing_pins` で拒否。`repair-pins` と `converge-bound-pair` は **bound** row を要求 (`not_hibernated_released_bound_pins_empty`) するので、binding が空な限り走れない。**回復方向の出口が無く**、残る経路は `--migrate-hibernated-legacy` / `--retire-hibernated-unbound-live-zero` / `--reconcile-hibernated-live` の terminal 化のみ = この shape の lane は**終端しかできない** | `sublane_worktree_binding_repair.py` の `BLOCK_MISSING_PINS`、`sublane_hibernated_pin_repair.py` docstring ("hibernated / released **BOUND** ... `worktree_identity` present")、`hibernated_bound_pair_convergence.py` の `BLOCK_NOT_BOUND_SIGNATURE` |
| **GAP-4** | `hibernated` row の server-restored pair を、**session を保存したまま** pin 宣言する経路 | #15811 が ACTIVE 側に作った metadata-only の adopt (close も launch もしない) に、hibernated 側の対応物が無い。同 shape で使えるのは `converge-bound-pair` = **pair を置換 (close + relaunch)** する破壊的経路だけ。復元された provider session を保存する選択肢が active 側にしか存在しない | `restored_pair_adopt.py` docstring vs `hibernated_bound_pair_convergence.py` help ("replace the exact stale/unattested pair ... then repair pins") |
| **GAP-5** | `audit-failure-terminal` が記録した監査失敗 lane の terminal retire | 記録は書けるが**何も authorize しない**。`coordinator_receipt_authority_unresolvable` で #15195 待ち。監査失敗 lane の終端は現在 owner 手動 | `cli_audit_failure_terminal_decision.py` help |

GAP-1 と GAP-5 は「レール乱立の副作用」ではなく **外部依存 (Herdr API / #15195) による既知残余**であり、
統合では解消しない。**GAP-2 / GAP-3 / GAP-4 は集合設計の欠落**であり、いずれも「個々のレールは
正しく fail-closed しているのに、集合として出口が無い」という #15811 と同型の形をしている。
特に GAP-3 は相互 precondition の deadlock であり、#15811 が埋めた class に最も近い。

### 3.2 overlap (同一 shape を複数レールが扱う)

| id | 重複するレール | 重複の実体 | 現時点で分かれている理由 (docstring 由来) |
| --- | --- | --- | --- |
| **OVERLAP-1** | `recover-stale` / `recover-gateway` / `refresh-worker` | 3 本とも #13806 tranche A/B の同一 actuation (guarded exact-generation close → same-slot launch → action-bound attestation → continuation 1 回)。`worker_turn_recovery` は `gateway_turn_recovery.classify_gateway_turn` を**逐語再利用**し、`TURN_CLASS_*` 語彙を共有 | 保護対象の集合が互いに反転している (gateway 保護 / worker 保護 / 両方保護)。staleness 述語が異なる (vanished vs live-unproductive)。#14661 j#92369 が「vanished worker と live-but-unproductive worker は別の事実で別の admission」と設計制約として固定 |
| **OVERLAP-2** | `repair-pins` / `converge-bound-pair` | subject signature が literal に同一 (`not_hibernated_released_bound_pins_empty`)。前者は「live pair から pin を書くだけ」、後者は「stale pair を置換してから pin を書く」 | actuation 予算が違う (metadata-only vs process 置換)。#13879 は #13847 の precondition を弱めないことを明示目的にしている |
| **OVERLAP-3** | `retire` の 6 intent + `--migrate-hibernated-legacy` + `--reconcile-hibernated-live` | すべて「metadata-only の terminal CAS」。差分は `(disposition × worktree_identity × liveness proof の出所)` の 3 軸のみ | 各 intent が異なる liveness authority を持つ。#14242 は「ACTIVE row は `process_release == not_requested` なので live-inventory 読取が唯一の liveness authority」であり #13845 より要求が高い、と明記。#14499 は「operator が 5 intent を 1 語彙で読めるよう #14242 を意図的に mirror した」と記録 |
| **OVERLAP-4** | `recover-pair-delivery` / `recover-worker-delivery` / `rehydrate-fleet --execute` の `restore_dispatch` | 3 経路が「元の implementation_request を再配送する」。前 2 者は `recovery_effect_contract` の applied-effect / unresolved-fate 契約を共有 | gateway 経由 / worker 直送 / fleet 単位、で経路と承認が異なる |
| **OVERLAP-5** | `reboot-audit` / `rehydrate-fleet` (plan) | 同一の per-lane joined facts に対する 2 つ目の per-lane 決定 | `fleet_rehydrate` docstring が明示: #14499 は「この lane はどの disposition へ収束すべきか」、#15745 は「この lane はどの未配送 action を負っているか」で**問いが違う**。#14499 の「lane ごとに違う答え」性質を潰さないため別 planner にした |
| **OVERLAP-6** | `quarantine` / `prepare-bound-pair` / `refresh-worker` | 3 本が「pending composer が前進を塞ぐ」状態を扱う | 承認 gate が別 (`quarantine` の 5 token / `bound_pair_composer_discard_approval` / `worker_refresh_owner_approval`)。lane disposition の前提も別 (任意 / hibernated-bound / active) |
| **OVERLAP-7** | `adopt-restored-pair` / `rebind-restored-pair` | 同一 subject (ACTIVE lane、server-restored pair、pin snapshot が現実と合わない) | pin snapshot が absent か stale かで CAS が別物 (empty-only backfill vs exact-2件 replace)。adopt は rebind より**厳しい** proof chain を要求する (declared pin という照合先が無いため) |

## 4. 統合提案

各候補には「**統合しても失ってはならない不変条件**」を、それを封じている regression test
つきで列挙する。Phase 2 の受け入れ条件は「下記 test が 1 本も緑を失わないこと」に落とせる。

**判断はしない**: どれを統合 / deprecate するかは coordinator / owner の escalation 対象
(`## 5`)。ここでは候補と、統合時に守るべき条件だけを記述する。

### C1. turn-failure 系 3 レールの単一 rail 化 (OVERLAP-1)

**候補**: `recover-stale` / `recover-gateway` / `refresh-worker` を、`--slot {gateway,worker}`
× `--staleness {vanished,live_unproductive}` を取る 1 rail へ折り畳む。

保存すべき不変条件:

| 不変条件 | 封じている事故 | test |
| --- | --- | --- |
| zero-close typed blocker: productive provider / tool-child、identity 不明、authority conflict、unreadable worktree、gateway / foreign slot、wrong issue-lane、stale generation のいずれでも**何も close しない** | j#79485 §2 の全 zero-close シナリオ | `tests/regressions/test_issue_13806_tranche_d_stale_worker_recovery.py`、`test_issue_13806_recover_stale_convergence.py` |
| replacement transaction の atomic + resumable 性 (close と launch の間の crash は、期待された `identity_unknown` + committed-close transaction のときだけ post-close resume として admit) | 部分置換の二重実行 | `test_issue_13806_replacement_transaction_core.py`、`..._tranche_b_replacement_actuator.py`、`..._tranche_c_self_replacement_completion.py` |
| gateway 側: **unconfirmed delivery / turn 未開始を failure に昇格しない** (#14219 で `delivered_not_started` 2 件が実は成功着地だった) | 生きている gateway の誤 close | `tests/regressions/test_issue_14203_gateway_refresh.py`、`test_issue_14485_recover_gateway_v1_verify.py` |
| worker 側: `recover-stale` の `not_stale` fence を**緩めない** (vanished と live-but-unproductive は別事実) | live worker の誤 close | `tests/regressions/test_issue_14661_worker_refresh.py` |
| worker refresh: dirty worktree の byte 保存 (close は 1 process だけを終わらせる) | in-scope な未 commit 差分の喪失 | 同上 |
| 承認は prose containment ではなく **exact 構造化 marker** (否定文 / 引用 retry command / log 行 / `:g30` が `:g3` を含む prefix 衝突の 4 経路をすべて拒否) | j#92487 F1 の 4 通りの未承認 close | 同上 (`worker_refresh_approval`) |
| resume は**既存 anchor の再配送のみ**。IR / Review Request を再生成しない | 二重起票 | `test_issue_14203_gateway_refresh.py` |

**統合を難しくする点** (Phase 2 で先に解く必要がある): 保護集合が互いに反転しているため、
「保護対象を parameter 化する」実装は、パラメータ 1 つの誤りで gateway を close できて
しまう。現状 3 本は**その誤りを型で不能にしている**。

### C2. hibernated-bound の pin repair 2 レールの統合 (OVERLAP-2)

**候補**: `repair-pins` を `converge-bound-pair --metadata-only` に吸収、または逆に
`converge-bound-pair` を `repair-pins` + 既存 pair 置換 rail の合成として再定義。

| 不変条件 | 封じている事故 | test |
| --- | --- | --- |
| stale locator を pin へ**昇格させない** (pin は最終的で unique で live で attested な pair からのみ書く) | 死んだ locator の pin 化 | `tests/regressions/test_issue_13933_bound_stale_pair_convergence.py` |
| bound signature の typed fault (9 種) がそれぞれ別語で報告される | 「なぜ拒否されたか」の消失 | `test_issue_13933_typed_bound_signature_faults.py` |
| `repair-pins` は byte-equal のみ idempotent。**異なる** set が既に pinned な row は zero-write refuse (上書きしない) | 別 pair での上書き | `test_issue_13879_hibernated_bound_pin_repair.py` |
| metadata-only 性: `repair-pins` は close / launch / resume / send path を**一切持たない**。`process_release` / `lane_generation` / `worktree_identity` / `replacement_*` / `reconcile_phase` を保存 | metadata 修復が process へ波及 | 同上 |
| `repair-pins` が mutable な `updated_at` を動かして直後の resume を `stale_generation` にしない (freshness anchor は immutable な `hibernated_at`) | #14477 の実測事故 (operator glass-break が必要になった) | `test_issue_14477_repair_pins_resume_freshness_anchor.py` |
| convergence は **pending composer を必ず保存**する (discard は別 gate の `prepare-bound-pair` のみ) | 未送信入力の喪失 | `test_issue_13933_hibernated_bound_pair_composer_discard.py` |

### C3. retire intent 群の宣言表化 (OVERLAP-3)

**候補**: 6 intent + 2 migration を廃止せず、`(disposition, worktree_identity, liveness proof)`
の宣言表 1 枚 + 共通 CAS へ折り畳む。intent flag は表への index として残す。

| 不変条件 | 封じている事故 | test |
| --- | --- | --- |
| zero-close は、durable row が**既に** `retired` のときだけ retire として通る (`zero_close_unproven`) | 何も閉じていない run が retire を主張 | `tests/regressions/test_issue_13754_retire_zero_close_fence.py` |
| ACTIVE row は `process_release == not_requested` のため live-inventory 読取が**唯一の** liveness authority。unreadable inventory は empty ではない / duplicate slot は inventory 不健全 / locator 無しは「解決不能」であって「不在」ではない / foreign occupant は実プロセス | #14222 j#85208 の shape で terminal 化を誤認 | `test_issue_14242_active_live_zero_retire.py` |
| launch race: revision fence は relaunch を見ない (`declare_active` は row を変えない) ため、launch/terminalize の排他 lock を action-time 半分に EXCLUSIVE で掛ける | j#85219 F1 の launch race | 同上、`test_issue_14499_reboot_residue_convergence.py` |
| UNBOUND row では worktree attestation の**代用を発明しない**。caller が測定した `(lane_generation, revision)` を宣言し CAS がそれを要求する | 弱い保証を同じ名前で提供する | `test_issue_14716_hibernated_unbound_retire.py`、`test_issue_14499_reboot_residue_convergence.py` |
| bound retire: canonical worktree ↔ 実 branch 一致 (#13841 review j#79150 F1) | clean+integrated の証拠が別 head を指す | `test_issue_13845_hibernated_bound_live_zero_retire.py`、`test_issue_13841_hibernated_legacy_retire_migration.py` |
| foreign-only-live-inventory: 予期しない provider だけが占有する unit は live 0 と測れるが quiescent ではない | #13845 j#80123 の実測 | `test_issue_13897_hibernated_legacy_foreign_inventory.py` |
| worktree identity family の一貫性 | 同名 branch を持つ他 repo の token 混入 | `test_issue_14715_retire_worktree_identity_family.py`、`test_issue_14475_lane_worktree_binding_fence.py` |
| retirement cleanup は **Git 操作を一切行わない** (remote branch delete / local branch delete / worktree removal の 3 つは R7-R9 で全撤回済み。「操作自身が安全条件を強制できない操作は提供しない」) | j#96344 / j#96396 / j#96401 で実測された commit / foreign checkout の破壊 | `tests/unit/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/test_retirement_cleanup_policy.py` |

### C4. 復元 pair 2 レールの統合 (OVERLAP-7)

**候補**: `adopt-restored-pair` と `rebind-restored-pair` を「pin snapshot の現在値で分岐する
1 rail」に折り畳む。両者は既に status 語彙と `slot_reason` を共有している。

| 不変条件 | 封じている事故 | test |
| --- | --- | --- |
| adopt の subject は **exactly absent** のみ。非空 snapshot は劣化していても上書きしない (証拠の破壊防止、"pin unresolvable" が "recover anything" と読まれない) | GAP-2 の劣化 snapshot を無言で潰す | `tests/regressions/test_issue_15811_cold_pair_recovery.py`、`tests/scenarios/test_issue_15811_cold_pair_adopt_acceptance.py` |
| adopt は attested launch-generation row を**必須**とする (rebind の pre-#15769 attestation-only fall-through を持たない)。照合先の declared pin が無いため | server 名以外に live process と lane を結ぶものが無い状態での誤 adopt | 同上 |
| slot は caller 供給名ではなく **server-owned `mzb1` 名の decode** で選ぶ | caller による slot 詐称 | 同上 |
| write は action 時に**自分の証拠を再導出**し、outcome は preflight ではなく **action-time の plan** を返す (review j#109452 `finding_actiontimeoutcome`: `w1:%1` を報告しながら書いた pin は `w9:%11` だった) | 監査証拠と実書込の乖離 | 同上 |
| rebind: locator drift が**実在する**ことを要求 (`locator_not_drifted` / `declared_locator_still_live`) | drift していない pair の無用な置換 | `test_issue_15656_rebind_restored_pair.py` |
| rebind: terminal id / locator の old→new lineage を outcome に記録 | 追跡不能な再 attest | `test_issue_15769_restored_pair_reattest.py` |

### C5. 再配送 3 経路の統合 (OVERLAP-4)

| 不変条件 | 封じている事故 | test |
| --- | --- | --- |
| applied-effect と unresolved-fate を混同しない。`redispatch_uncertain` は「outbox reserve 前の zero-write refusal」と「send 後の不明 fate」の両方を含むので、applied effect と呼ぶと status が支えない write を主張することになる | j#88538 / j#88554 / j#88563 の 3 回のレビュー往復 | `recovery_effect_contract` の validator と `tests/regressions/test_issue_14203_recovered_worker_delivery.py` / `..._pair_recovery_anchor_delivery.py` |
| 「何か変わったか」を「行動したか」の代理にしない (first-close 失敗は何も適用しないが blocked である) | 失敗 run が非 blocked と読まれる | 同上 |
| `rehydrate-fleet`: `uncertain_partial` と unreadable ledger は **block であって retry ではない**。配送済み key は再利用しない | blind replay による二重配送 | `tests/unit/.../test_fleet_rehydrate.py`、`tests/scenarios/test_issue_15745_fleet_rehydrate_acceptance.py` |
| plan の effect budget = 0 (transaction を開かず fence を予約せず破壊的 step を名指さない) | plan が副作用を持つ | 同上 |
| 受信側 admission: **1 read** で key と supersede verdict を取る (2 read だと間に落ちた gate が見えない = #13889 R2-F1 の TOCTOU)。検証は claim の**前** (superseded な recovery が key を焼かない) | 二重 actuation / key の焼損 | `tests/unit/.../test_callback_recovery_key.py`、`tests/integration/.../test_callback_recovery_admission.py` |
| recovery key の length-prefixed canonical encoding (`name=<len>:<value>`) — 単純 delimiter join では `lane="a"`+`route="b:c"` と `lane="a:b"`+`route="c"` が同一 digest になる | 別 action が duplicate として無言 no-op | `test_callback_recovery_key.py` |

### C6. 診断 2 レールの統合 (OVERLAP-5) — **非推奨として記録**

`reboot-audit` と `rehydrate-fleet` plan の統合は、`fleet_rehydrate` docstring が
**明示的に避けた設計**である。統合すると #14499 の「reboot は lane ごとに違う答えを要求する
ので all-lanes action を持たない」という契約が、`rehydrate-fleet` の bulk 実行面と接触する。
本 doc は統合候補として挙げるが、**既存 docstring が理由つきで否定している**ことを記録し、
Phase 2 で採るなら ADR-0001 の「記録済み設計判断の反転には owner 承認が要る」に該当する
可能性を明示する。

## 5. escalation 対象 (本 US では判断しない)

以下は `design_consultation` として coordinator / owner へ上げる。

1. **GAP-1 (`recover-restored-pair` が構造的に常時 block)** の扱い: (a) Herdr へ
   generation-conditional close primitive を要求する、(b) 別の証明で actuation を admit する、
   (c) 診断専用として明示 deprecate する、のいずれか。現状は「実行できないレールが
   実行可能に見える」状態である。
2. **GAP-2 (劣化 pin snapshot に subject が無い)** を埋めるか、明示的に「owner 判断案件」
   として固定するか。埋める場合、劣化 snapshot の証拠保存と修復の両立が設計課題になる。
3. **GAP-3 (hibernated + unbound + pins absent の deadlock)** の扱い: この shape を
   回復可能にするか、「終端専用と認める」ことを明示決定するか。現状は暗黙の終端専用である。
4. **GAP-4 (hibernated 側に metadata-only adopt が無い)** を埋めるか。埋めない場合、
   hibernated lane の復元 session は破壊的置換でしか回収できないことを明示記録する。
5. **C1 の統合可否**: 型で不能にしている保護反転を parameter 化する取引を受け入れるか。
6. **C3 の統合可否**: intent flag の operator 可読性 (#14499 が意図的に mirror した性質) を
   宣言表に畳んだあとも維持できるか。
7. **C6 が ADR-0001 の反転に当たるか**の判定。
8. Phase 2 の受け入れ条件を「本 doc の C1-C5 に列挙した test が 1 本も緑を失わない」で
   固定してよいか。

## 参照

- ADR-0011 (3階層の責務分担) / ADR-0001 (owner 決定の ADR 記録)
- `vibes/docs/logics/managed-state-model.md` (lifecycle 正本)
- `vibes/docs/specs/herdr-native-identity.md` / `vibes/docs/specs/route-identity-ledger.md`
- `src/mozyo_bridge/core/state/lane_pin_role.py` (`PIN_PAIR_*` 語彙の正本)
- `src/mozyo_bridge/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/`
  (全レールの application / domain)
