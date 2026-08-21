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
回収 / 終端に関与する **25 本**。「発生源」は各 module docstring が名指す実測事象。

**本数の数え方 (曖昧さを残さないため明記する)**: 下記 5 表は **33 行**あり、distinct な
top-level command は **26 本**である。差は次の 2 つ。

- `retire` は 1 command で、`--migrate-hibernated-legacy` / `--reconcile-hibernated-live` /
  `--retire-hibernated-bound` / `--retire-active-live-zero` / `--retire-active-unbound-live-zero` /
  `--retire-hibernated-unbound-live-zero` の 6 intent + 既定を **7 行**に展開している。
- `rehydrate-fleet` は plan (1.A) と `--execute` (1.D) で **2 行**に展開している。

26 本のうち **`list` は回収レールではない** — pane inventory の advisory な一般 diagnosis
surface で、retire / kill / route を一切行わない。比較のために表へ載せるが**本数には数えない**。
したがって「回収 / 終端レール = 25 本」であり、`list` を含めた表の distinct command は 26 本。

### 1.A 診断レール (read-only、状態を書かない)

| レール | 何をするか | 前提 pair shape | 発生源 | 出力 disposition | 主な typed refusal |
| --- | --- | --- | --- | --- | --- |
| `reboot-audit` | Redmine / git / lifecycle row / live inventory の 4 authority を **1 snapshot** で join し、lane ごとの次レールを typed で返す | 任意 (全 lane 走査) | host reboot (#14499、実測 #13490 j#89060: 23 pane 中 15 が shell residue) | `restore_worktree` / `terminalize_bound_metadata` / `terminalize_unbound_metadata` / `close_shell_residue` / `guarded_close` / `resume` / `hibernate` / `supersede` / `already_terminal` / `unknown` / `blocked` | `issue_state_unreadable` / `inventory_unreadable` / `worktree_presence_unknown` / `head_not_integrated` / `foreign_occupant` |
| `rehydrate-fleet` (plan) | manifest が active と呼ぶ lane 群に対し「未配送の action」を per-lane で決める。plan の effect budget は 0 | ACTIVE row + OPEN issue | host restart で全 pane の attestation 失効。`herdr session-start` は default pair しか戻さない (#15745) | `heal_pair` / `restore_dispatch` / `resume_brief`、または typed skip / block | `dispatch_uncertain` / `dispatch_record_unreadable` / `startup_interaction_required` / `foreign_slot` / `lane_moved` |
| `quarantine-inspect` | 1 role の assigned name / locator / revision / attested generation / quarantine action id / generation mismatch 軸を報告し、貼付可能な owner approval を render | 任意の managed receiver | approval に必要な 5 token が公開 read 面から取得できず #14163 の 6-lane drain が停止 (#14234) | `ready` + approval template、または typed refusal | `workspace_unresolved` / `inventory_unreadable` / `composer_unreadable` / `duplicate_receiver` / `not_quarantine_candidate` |
| `callback-recovery` | delivered-but-quiet な作業単位を durable-record の事実から 4 つの callback-stall class に分類し、標準回収経路を出力 | 任意 | callback が送られたのに durable gate が落ちない (#12159 / #13520) | 4 class + 回収経路。genuine stall で非 0 exit | — (read-only、権限変更なし) |
| `recover-restored-pair` | reboot 復元で cwd / startup identity proof が不整合になった ACTIVE lane の exact idle 世代を検査 | ACTIVE + pin あり + 両 slot live + cwd drift または attestation non-green | reboot restoration (#15227) | **preflight のみ** (下記 GAP-1 参照) | `managed_slot_busy` / `managed_pair_already_healthy` / `pending_composer_loss_not_approved` / `generation_conditional_close_unavailable` |
| `list` (**回収レールではない / 本数外**) | pane inventory から live sublane を列挙し stale/retire hint を出す | 任意 | 日常運用 | advisory hint のみ。retire / kill / route は一切しない | — |

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

### 1.E lifecycle disposition レール (disposition CAS が主。付随して process release / guarded close を伴うものがある)

**effect budget は family 内で一様ではない**ので、行ごとに読むこと。metadata-only (disposition CAS
だけで process を触らない) は `resume` と `retire` の 6 intent。`hibernate` と `supersede` は CAS の
あとに **process release** を行い、既定の `retire --execute` は **guarded close** を行う。
`audit-failure-terminal record` は診断記録のみで CAS すら行わない。

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

**すべての軸は「読めなかった = `unknown`」を値と区別する** (`unknown` は必ず block へ落ちる。
`reboot_residue_convergence` の "Unknown is not absence"、`fleet_rehydrate` の
"An axis that could not be read is None and yields a block")。

**`unknown` と `not_applicable` は別物である。** `unknown` は「読もうとしたが読めなかった」、
`not_applicable` は「実装がその gate を評価する前に short-circuit したので観測が存在しない」。
前者は block、後者は**その条件を空虚に満たす** (`## 2.3`)。

**この軸集合は `## 3` の全 rule が参照する観測の閉集合である。** ただし**閉包は必要条件であって
十分条件ではない** — 軸が閉じていても、rail の admission gate を条件として符号化し忘れれば
決定木は拒否 shape を推奨する (round 4 review j#109763 `finding_railadmissionclosure`)。
`## 3.3` の検算 1-3 は前者を、検算 10 (rail ごとの gate 対応表) は後者を見る。

```yaml
pair_shape:

  # --- A. durable lifecycle row 由来 ---
  disposition:        [active, hibernated, retired, superseded, unknown]
  worktree_identity:  [bound, empty, unknown]
  declared_pins:      [resolvable, absent, degraded, unknown]   # degraded = unreadable|foreign_pin_role|mixed_pin_role_vocabulary|duplicate_pin_role|incomplete_pin_pair
  process_release:    [not_requested, in_flight, released, unknown]

  # --- B. live inventory 由来 ---
  live_pair:          [both_live, half_live, zero_live_positive, shell_residue, foreign_occupant, unknown]

  # slot_health は role ごとに直交事実の直積。実装の observation field と 1:1 に対応させる
  # (導出しない — 導出すると round 4 `finding_slotverdictclosure` の非全域化が起きる)。
  slot_health:
    gateway: &slot_facts
      liveness:              [live, vanished, shell_residue, unknown]
      membership:            [this_pair, foreign, ambiguous, not_applicable, unknown]
      attestation:           [live_joined, restore_stale, stale, absent, not_applicable, unknown]
      launch_generation_row: [attested_bound, pending, superseded, absent, not_applicable, unknown]
      participant_lineage:   [joined, absent, not_applicable, unknown]
      generation_rank:       [current, newer, older, unknown]
      productivity:          [productive, turn_ended_unproductive, busy, idle, not_applicable, unknown]
      composer:              [settled, pending, not_applicable, unknown]
      cwd:                   [matches, drifted, unreadable, not_applicable, unknown]
      locator:               [pinned_match, drifted, unresolved, not_applicable, unknown]
      worktree:              [readable, unreadable, not_applicable, unknown]
      # 実装の独立 positive fact。導出値ではない (decide_slot_recovery が直接読む)
      already_healthy:       [true, false, not_applicable, unknown]
      bad_generation_signal: [positive, absent, not_applicable, unknown]
      # 1 delivered callback の provider turn 分類 (gateway_turn_recovery の TURN_CLASS_*)
      turn_class:            [turn_productive, turn_failed_no_durable_gate, turn_unconfirmed,
                              turn_not_settled, turn_unobservable, not_applicable, unknown]
    worker: *slot_facts

  # --- C. Redmine / git 由来 ---
  issue_state:        [open, closed, unknown]
  head_integrated:    [true, false, unknown]

  # --- D. durable record 由来の判定軸 (lane 単位) ---
  stale_signal:       [positive, negative, unknown]   # #13518 shell-residue の positive 判定 (`recover-stale` の is_stale gate)
  resume_anchor:      [present, absent, unknown]      # refresh 後に再配送する durable anchor (`no_resume_anchor` gate)
  dispatch:           [owed, delivered, uncertain, unreadable, attribution_unknown, not_applicable]
  park_basis:         [dependency_park, early_hibernate, absent, unknown]
  resume_gates:       [green, blocked, unknown]       # `resume` の release-settled / issue-reowned / generation fence の総合
  successor_attested: [true, false, unknown]
  successor_same_issue: [true, false, unknown]
  original_idle:      [true, false, unknown]
  single_slot_mode:   [requested, not_requested]      # rebind の `--allow-single-slot`
  rebind_actionability: [repairs_needed, already_current, unknown]  # locator drift とは独立。terminal/participant/receipt/attestation の要修復有無
  issue_lane_match:   [true, false, unknown]          # `wrong_issue_lane` gate
  launch_authority:   [current, unavailable, unknown] # `launch_authority_unavailable` gate
  counterpart_distinguished: [true, false, unknown]   # `worker_not_distinguished` / `gateway_not_distinguished` gate
  authority_conflict: [none, present, unknown]        # `authority_conflict` gate
  dispatch_anchor:    [present, absent, unknown]      # LaneDispatchFact.sendable は anchor_journal の非空も要求する

  # --- E. 配送 authority。action-scoped な valid-for 述語 ---
  delivery_authority:
    recovery_anchor_authorization: [valid_for_this_anchor, mismatched, absent, unknown]
    zero_send_evidence:            [valid_for_this_action, mismatched, absent, unknown]
    target_generation_pin:         [exact_live_generation, stale, absent, unknown]
    lifecycle_decision_journal:    [valid_for_this_lane, mismatched, absent, unknown]
```

軸の出所と、間違えやすい区別:

- `liveness: shell_residue` と `liveness: vanished` は**別物**。`recover-stale` の subject は
  **存在する** assigned-name row が `classify_named_slot(row) == SLOT_STALE` になる
  shell-residue であり、row 自体が消えた `vanished` ではない
  (`RECOVER_BLOCK_NOT_STALE` の定義文: "Only a genuine residue is recovered (never a live worker)")。
  一方 `recover-pair` / `converge-bound-pair` の `slot_absent` short-circuit は `vanished` の側。
- `productivity: turn_ended_unproductive` (process は live) / `liveness: vanished` (row ごと消えた) /
  `liveness: shell_residue` (row は在るが agent がいない) の 3 つを混同しない。
- `already_healthy` / `bad_generation_signal` は `decide_slot_recovery` が**独立に読む positive fact**
  であり、attestation / cwd / locator から導出してはならない。
- `attestation: absent` は adopt / rebind いずれでも**拒否側の値**であり admit 値ではない。
- `turn_class` は `gateway_turn_recovery` の閉語彙。`turn_failed_no_durable_gate` 以外は
  `turn_not_classified_failed` で拒否される。
- `declared_pins` の値語彙は `core/state/lane_pin_role.py` の `PIN_PAIR_*` 正本。
- `dispatch` の値語彙は `domain/fleet_rehydrate.py` の `DISPATCH_*` 正本。
- `delivery_authority` の 4 軸は `RecoveryDeliveryAuthorization` / `RecoveryDeliveryZeroSendEvidence` /
  `RecoveryAnchorDeliveryRequest` / `RecoveredWorkerDeliveryRequest` の field から導いた。
  **ただし各 rail が実際に読む軸は異なる** (`## 3.1d`)。

### 2.1 派生値: `slot_verdict` (全域・決定的)

`decide_slot_recovery` (`domain/hibernated_pair_recovery.py`) の順序評価を写したもの。
**正本は実装側。本 doc は対応表である。** 全分岐が単一の結果を返す (round 4 review
`finding_slotverdictclosure` の訂正: 以前の版は step 9 で 2 結果を残していた)。

```text
slot_verdict(slot):        # 上から最初に成立したもの。すべて単一結果
  1. liveness == vanished:
       generation_rank != newer  -> recover_bad_generation      # short-circuit。以降の gate は評価されない
       else                      -> preserve_newer_generation
  2. membership in [ambiguous, unknown]        -> preserve_ambiguous
  3. membership == foreign                     -> preserve_foreign
  4. generation_rank == newer                  -> preserve_newer_generation
  5. productivity in [productive, busy]        -> preserve_productive   # 実装の not_productive は runtime==busy を False にする
  6. composer == pending                       -> preserve_pending_composer
  7. worktree == unreadable                    -> preserve_worktree_unreadable
  8. already_healthy == true                   -> healthy_no_action        # 独立 fact。導出しない
  9. bad_generation_signal == positive         -> recover_bad_generation
 10. otherwise                                 -> preserve_ambiguous       # 陽性の残渣信号が無ければ保存
```

値域は
`[recover_bad_generation, healthy_no_action, preserve_ambiguous, preserve_foreign,
preserve_newer_generation, preserve_productive, preserve_pending_composer,
preserve_worktree_unreadable]`。`slot_verdict` は rule の条件 key として使ってよい。

### 2.2 rail ごとの admission 契約 (`when` は routing であって admit の証明ではない)

**決定木は routing を決めるだけで、admit を主張しない。** 各 rail の最終判定は実装の
ordered gate が行う。`when` はその rail を**候補として選ぶための discriminator** であり、
実装の refusal 集合を再現したものではない。`--execute` の前には必ず当該 rail の preflight を
実行し、typed refusal に従うこと。

この契約を置く理由は 2 つある。(a) 下表 16 rail の admission 語彙は実測で **合計 266 token
(distinct 192)** あり、
決定木がそれを全再現すると**実装と doc の二重正本**になって drift 源になる (本 doc が Phase 1 で
避けようとしている形そのもの)。(b) round 3-5 で「gate を手で選んで書き足す」方式を続けた結果、
毎 round 取り落としが見つかった (`finding_railadmissionclosure` / `finding_deliverysendability`) —
**手選択である限り完全性は担保できない**。

そこで doc は各 rail について「どの module が admission の正本か」「その refusal 語彙が何 token あるか」
「決定木が routing のために符号化した軸はどれか」を宣言し、`## 3.3` の検算 13 / 14 が
**実装 source から token を再抽出して count を照合**し、**符号化したと宣言した軸が実際に
`when` にあるか**を検算する。gate の完全性は主張せず、**正本の所在と符号化の実在**を機械保証する。

```yaml
rail_admission:
  - rail: adopt-restored-pair
    rule: R2
    admission_source: ['domain/restored_pair_adopt.py']
    admission_gate_count: 19
    routing_conditions_encoded: ['disposition', 'declared_pins', 'live_pair', 'liveness', 'membership', 'attestation', 'launch_generation_row', 'participant_lineage']
  - rail: rebind-restored-pair
    rule: R3a
    admission_source: ['domain/restored_pair_rebind.py']
    admission_gate_count: 34
    routing_conditions_encoded: ['disposition', 'declared_pins', 'single_slot_mode', 'rebind_actionability', 'liveness', 'membership', 'attestation']
  - rail: recover-restored-pair
    rule: R4
    admission_source: ['domain/restored_pair_recovery.py']
    admission_gate_count: 15
    routing_conditions_encoded: ['disposition', 'declared_pins', 'liveness', 'membership', 'cwd']
  - rail: recover-gateway
    rule: R5
    admission_source: ['domain/gateway_turn_recovery.py']
    admission_gate_count: 17
    routing_conditions_encoded: ['disposition', 'resume_anchor', 'issue_lane_match', 'launch_authority', 'counterpart_distinguished', 'authority_conflict', 'liveness', 'membership', 'generation_rank', 'productivity', 'composer', 'turn_class']
  - rail: refresh-worker
    rule: R6
    admission_source: ['domain/worker_turn_recovery.py', 'domain/gateway_turn_recovery.py']
    admission_gate_count: 19
    routing_conditions_encoded: ['disposition', 'resume_anchor', 'issue_lane_match', 'launch_authority', 'counterpart_distinguished', 'authority_conflict', 'liveness', 'membership', 'generation_rank', 'productivity', 'composer', 'turn_class', 'worktree']
  - rail: recover-stale
    rule: R7
    admission_source: ['domain/stale_worker_recovery.py']
    admission_gate_count: 9
    routing_conditions_encoded: ['disposition', 'stale_signal', 'issue_lane_match', 'counterpart_distinguished', 'authority_conflict', 'liveness', 'membership', 'generation_rank', 'productivity', 'worktree']
  - rail: recover-pair
    rule: R12
    admission_source: ['domain/hibernated_pair_recovery.py']
    admission_gate_count: 8
    routing_conditions_encoded: ['disposition', 'declared_pins', 'slot_verdict']
  - rail: repair-pins
    rule: R9a
    admission_source: ['application/sublane_hibernated_pin_repair.py']
    admission_gate_count: 13
    routing_conditions_encoded: ['disposition', 'worktree_identity', 'declared_pins', 'process_release', 'slot_verdict']
  - rail: repair-worktree-binding
    rule: R11
    admission_source: ['application/sublane_worktree_binding_repair.py']
    admission_gate_count: 22
    routing_conditions_encoded: ['disposition', 'worktree_identity', 'declared_pins', 'process_release']
  - rail: converge-bound-pair
    rule: R9b
    admission_source: ['domain/hibernated_bound_pair_convergence.py']
    admission_gate_count: 23
    routing_conditions_encoded: ['disposition', 'worktree_identity', 'declared_pins', 'process_release', 'slot_verdict']
  - rail: prepare-bound-pair
    rule: R10
    admission_source: ['domain/hibernated_bound_pair_composer_discard.py']
    admission_gate_count: 18
    routing_conditions_encoded: ['disposition', 'worktree_identity', 'declared_pins', 'slot_verdict']
  - rail: close-residue
    rule: P1
    admission_source: ['application/sublane_residue_close.py']
    admission_gate_count: 16
    routing_conditions_encoded: ['live_pair', 'liveness']
  - rail: supersede
    rule: T3
    admission_source: ['application/sublane_supersede.py']
    admission_gate_count: 4
    routing_conditions_encoded: ['disposition', 'successor_attested', 'successor_same_issue', 'original_idle']
  - rail: retire
    rule: T1
    admission_source: ['application/sublane_retire_application.py']
    admission_gate_count: 10
    routing_conditions_encoded: ['issue_state', 'head_integrated']
  - rail: rehydrate-fleet
    rule: D3
    admission_source: ['domain/fleet_rehydrate.py']
    admission_gate_count: 32
    routing_conditions_encoded: ['dispatch_anchor']
  - rail: resume
    rule: R13
    admission_source: ['application/sublane_resume.py']
    admission_gate_count: 7
    routing_conditions_encoded: ['disposition', 'live_pair', 'issue_state', 'resume_gates']
```

`admission_gate_count` は base `289343db` 時点の実測値。実装が変われば検算 13 が落ちるので、
**doc の drift はここで可視化される** (drift の自動追随は本 US scope 外。`## 5` escalation 参照)。

### 2.3 `not_applicable` の規約 (short-circuit された gate)

実装は `liveness: vanished` の slot について membership / attestation / launch_generation_row /
participant_lineage / productivity / composer / cwd / locator / worktree / already_healthy /
bad_generation_signal / turn_class を評価しない (`decide_slot_recovery` の step 1 が return し、
live 側の観測も `SlotRecoveryObservation(slot_absent=True, generation_not_newer=...)` だけを設定する)。

- そのとき上記の軸は `not_applicable` とする。**`unknown` にしてはならない** — `unknown` は
  P0 の診断 block へ落ちるので、実装が正常に admit する partial-close replay が block と誤符号化される。
- `all_slots` / `any_slot` / `slot_health.<role>` の条件照合において、**`not_applicable` はその条件を
  空虚に満たす** (skip)。`any_slot` の存在量化は `not_applicable` を「成立」に数えない。
- `liveness` と `generation_rank` は vanished slot でも観測されるので `not_applicable` を取らない。

## 3. 決定木

### 3.0 評価アルゴリズム (実行意味論)

**単一 first-match ではない。** `evaluate` は **ordered plan または escalation** を返す。

```text
evaluate(shape) -> Plan | Escalation

  also = []                                          # 共適用 rail の収集先 (明示初期化)

  # 1. precursor: 宣言順に評価し、match したものを steps へ (閉集合。§3.1a)
  steps = [p for p in precursors in declared order if match(p.when)]
  if any(p.blocks_further_evaluation for p in steps):
      return Escalation(reason=first such p.id, steps=steps)   # 再観測してからやり直す

  # 2. phase ごとに独立に first-match (§3.1b / §3.1c)
  recovery = first_match(rules where phase == recovery)     # 無ければ null
  terminal = first_match(rules where phase == terminal)     # 無ければ null

  # 3. known_intersections (§3.1f)。要素は rule id なので、id を該当 phase の rule へ解決してから
  #    その rule の `when` で判定する。D1-D3 は `when` を持たず `admission` を持つので、
  #    delivery rail は `admission` で判定する (下記 step 4 と同じ述語)。
  for x in known_intersections:
      hit = all(matches_rule(rid, shape) for rid in x.rules)   # matches_rule は when または admission を読む
      if not hit: continue
      if x.disposition == "escalation":
          return Escalation(reason=x.reason, steps=steps, candidates=x.rules)
      also += [rid for rid in x.rules if rid != id_of(recovery) and rid != id_of(terminal)]

  # 4. rail_set をもつ rule (§3.1d の R14) は、実装 admission 述語で適用可能な rail を全列挙
  if recovery has rail_set:
      recovery.applicable  = [r for r in rail_set if match(r.admission)]
      recovery.policy_order = rail_set.policy_preference   # policy であって排他ではない
      also += recovery.applicable[1:] in policy order      # 先頭以外は共適用として並置

  # 5. precedence_basis 由来の共適用 (§3.1g)。first-match が選ばなかった後続 rule のうち、
  #    先行 rule が `refused_by_later` **以外** の basis で precedence を主張しているものは
  #    依然として適用可能なので also へ載せる。
  if recovery is not null:
      for e in recovery.precedence_basis or []:
          if e.basis == "refused_by_later": continue        # 後続は実装で拒否される。載せない
          also += [rid for rid in e.over if matches_rule(rid, shape)]

  # 6. selection table (§3.1e) を上から評価し、最初に match した行で決める
  sel = first_match(selection)
  if sel.result == escalation:
      return Escalation(reason=sel.reason, steps=steps, recovery=recovery, terminal=terminal)
  primary = recovery if sel.primary == "recovery" else terminal
  if primary is null: primary = fallback_from(sel)          # 各行の閉集合から
  if primary is null: return Escalation(reason="no_applicable_rail", steps=steps)

  # 7. terminal の sub-selection (§3.1c T1 の intent_by) も全域でなければ escalation
  if primary is T1:
      intent = first_match(T1.intent_by)
      if intent is catch_all_escalation: return Escalation(reason=intent.reason, steps=steps)

  return Plan(steps=steps, selected=primary, also_applicable=dedup(also))

matches_rule(rule_id, shape):
  r = lookup(rule_id)                                  # recovery / terminal / delivery のいずれか
  return match(r.when) if r has `when` else match(r.admission)

**この版で確定させたこと**:

- **実装が排他にしていないものを doc で排他にしない。** rule 間の排他は、実装が実際に読む軸の
  値が disjoint であるときだけ主張する。そうでない交差は `## 3.1f` の `known_intersections` に
  列挙し、`co_applicable` (併記する) か `escalation` (自動選択しない) のどちらかで閉じる。
  round 3 / round 4 の `finding_deliverypartition` は、いずれも実装に無い条件を doc が創作して
  排他を偽装したことによる欠陥だった。
- **policy 上の選択順序と実装 admission は別物。** 前者は `policy_preference` として、後者は
  `admission` として別 field に置く。
- precursor は `## 3.1a` の**閉集合**。selection は `## 3.1e` の table。散文ではない。
- どの phase にも該当しない shape、`closed` かつ `head_integrated: false`、既知交差のうち
  `escalation` のものは **escalation であって自動選択しない**。

**量化子**: `all_slots` は gateway と worker の両方、`any_slot` は少なくとも一方。
`slot_health.<role>` は role 名指し。`not_applicable` の扱いは `## 2.3`。

### 3.1a precursor (閉集合)

```yaml
precursors:
  - id: P0
    order: 0
    when: {any_axis: unknown}
    step: reboot-audit            # または rehydrate-fleet plan
    blocks_further_evaluation: true
    rationale: "読めない軸がある間は回収レールを選ばない。診断して再観測してからやり直す。"

  - id: P1
    order: 1
    when:
      live_pair: shell_residue
      all_slots: {liveness: [shell_residue, vanished]}   # live half が 1 つでもあれば close-residue は plan 全体を 0 にする
    step: close-residue
    blocks_further_evaluation: false
    rationale: >
      close-residue は **whole-unit** の residue を閉じる rail であり、live half が残っていると
      zero-close refusal になる。したがって aggregate `live_pair: shell_residue` だけでは足りず、
      両 slot が live でないことを条件に含める。片側だけ residue で相方が live な shape は
      P1 ではなく R7 (`recover-stale`) の subject である (round 4 `finding_staleresidueclassification`)。
```

### 3.1b recovery phase の rule 列

```yaml
recovery_rules:

  - id: R2
    when:
      disposition: active
      declared_pins: absent
      live_pair: both_live
      all_slots:
        liveness: live
        membership: this_pair
        attestation: [live_joined, restore_stale]   # absent は hard refusal
        launch_generation_row: attested_bound       # 必須。fall-through 無し
        participant_lineage: joined                 # 必須
    rail: adopt-restored-pair
    precedence_basis: [{over: [R8, R5, R6, R7, R14], basis: least_effect_first}]
    effect_budget: metadata_only

  - id: R3b
    when:
      disposition: active
      declared_pins: resolvable
      single_slot_mode: requested
      rebind_actionability: repairs_needed          # 全部 current なら pair-level `terminal_unchanged_noop` で拒否される
      any_slot: {liveness: vanished}                # typed `missing_live_slot` として admit される
      any_slot_other: {liveness: live, membership: this_pair,
                       attestation: [live_joined, restore_stale]}
    rail: rebind-restored-pair
    precedence_basis: [{over: [R8, R5, R6, R7, R14], basis: least_effect_first}]
    mode: single_slot
    effect_budget: metadata_only
    note: "`--allow-single-slot`。片 slot 完全不在を許し、生存 slot だけを re-pin / re-attest する。"

  - id: R3a
    when:
      disposition: active
      declared_pins: resolvable
      single_slot_mode: not_requested
      rebind_actionability: repairs_needed
      all_slots: {liveness: live, membership: this_pair,
                  attestation: [live_joined, restore_stale]}
    rail: rebind-restored-pair
    precedence_basis: [{over: [R4, R8, R5, R6, R7, R14], basis: least_effect_first}]
    mode: pair
    effect_budget: metadata_only
    note: >
      既定の pair mode。`attestation: absent` は `unattested_slot` で拒否されるので条件から除外する。
      **`locator: drifted` は要求しない** — 実装は locator が同一でも terminal / participant /
      receipt / attestation の restore-stale repair があれば admit する (#15769 回帰 test の
      `old_locator == new_locator` ケース)。actionability は `rebind_actionability` 軸で表す。
      R2 と違い `launch_generation_row: attested_bound` は必須でない — pre-#15769 の
      attestation-only fall-through を許す rail であるため。

  - id: R4
    when:
      disposition: active
      declared_pins: resolvable
      all_slots: {liveness: live, membership: this_pair}
      any_slot: {cwd: drifted}
    rail: recover-restored-pair
    precedence_basis: [{over: [R8, R5, R6, R7, R14], basis: diagnose_only_first}]
    status: diagnose_only         # GAP-1: 実行経路が構造的に存在しない
    effect_budget: none

  - id: R8
    when:
      disposition: active
      any_slot: {composer: pending}
    rail: quarantine
    precedence_basis: [{over: [R5, R6, R7], basis: refused_by_later},
                       {over: [R14], basis: precondition_of_later}]
    requires: quarantine-inspect
    note: "pending composer を扱う唯一の recovery rail。R5 / R6 / R7 は実装側で `pending_composer_input` の zero-close refusal になるので先に置く。"

  - id: R5
    when:
      disposition: active
      resume_anchor: present
      issue_lane_match: true
      launch_authority: current
      counterpart_distinguished: true
      authority_conflict: none
      slot_health.gateway:
        {liveness: live, membership: this_pair, generation_rank: current,
         productivity: turn_ended_unproductive, composer: settled,
         turn_class: turn_failed_no_durable_gate}
    rail: recover-gateway
    precedence_basis: [{over: [R6, R7], basis: role_precedence},
                       {over: [R14], basis: precondition_of_later}]
    protects: [worker, default_coordinator, foreign]

  - id: R6
    when:
      disposition: active
      resume_anchor: present
      issue_lane_match: true
      launch_authority: current
      counterpart_distinguished: true
      authority_conflict: none
      slot_health.worker:
        {liveness: live, membership: this_pair, generation_rank: current,
         productivity: turn_ended_unproductive, composer: settled,
         turn_class: turn_failed_no_durable_gate, worktree: readable}
    rail: refresh-worker
    precedence_basis: [{over: [R14], basis: precondition_of_later}]
    protects: [gateway, default_coordinator, foreign]

  - id: R7
    when:
      disposition: active
      stale_signal: positive
      issue_lane_match: true
      counterpart_distinguished: true
      authority_conflict: none
      slot_health.worker:
        {liveness: shell_residue, membership: this_pair, generation_rank: current,
         productivity: [idle, not_applicable], worktree: readable}
    rail: recover-stale
    precedence_basis: [{over: [R14], basis: precondition_of_later}]
    protects: [gateway, default_coordinator, foreign]
    note: >
      subject は **存在する** assigned-name row が `classify_named_slot(row) == SLOT_STALE` に
      なる shell residue。row ごと消えた `vanished` ではない。gateway が live のままでもよい
      (P1 の whole-unit close とはここで分かれる)。

  - id: R9a
    when:
      disposition: hibernated
      worktree_identity: bound
      declared_pins: absent
      process_release: released
      all_slots: {slot_verdict: healthy_no_action}
    rail: repair-pins
    precedence_basis: [{over: [R13], basis: precondition_of_later}]
    effect_budget: metadata_only

  - id: R9b
    when:
      disposition: hibernated
      worktree_identity: bound
      declared_pins: absent
      process_release: released
      all_slots: {slot_verdict: [recover_bad_generation, healthy_no_action]}
      any_slot:  {slot_verdict: recover_bad_generation}
    rail: converge-bound-pair
    precedence_basis: [{over: [R13], basis: precondition_of_later}]
    effect_budget: replace_bad_slots_then_repair_pins
    note: >
      実装の admit 条件と 1:1。片側 healthy + 片側 bad も、片側 vanished (partial close replay) +
      片側 healthy も正規の subject。healthy な slot は保存され bad な slot だけが置換される。

  - id: R10
    when:
      disposition: hibernated
      worktree_identity: bound
      declared_pins: absent
      any_slot: {slot_verdict: preserve_pending_composer}
    rail: prepare-bound-pair
    precedence_basis: [{over: [R13], basis: precondition_of_later}]
    then: converge-bound-pair

  - id: R11
    when:
      disposition: hibernated
      worktree_identity: empty
      declared_pins: resolvable
      process_release: released
    rail: repair-worktree-binding
    precedence_basis: [{over: [R12, R13], basis: precondition_of_later}]
    effect_budget: metadata_only

  - id: R12
    when:
      disposition: hibernated
      declared_pins: resolvable
      any_slot: {slot_verdict: recover_bad_generation}
    rail: recover-pair
    precedence_basis: [{over: [R13], basis: precondition_of_later}]
    effect_budget: replace_bad_slots_then_redispatch

  - id: R13
    when:
      disposition: hibernated
      live_pair: both_live
      issue_state: open
      resume_gates: green
    rail: resume
    effect_budget: disposition_cas_only

  - id: R14
    when: {disposition: active, issue_state: open, dispatch: owed}
    rail_set: delivery_rails        # 単一 rail ではない。§3.1d で適用可能なものを全列挙する
    note: >
      3 つの配送 rail は**実装上互いに排他ではない**。どれが適用可能かは各 rail 固有の
      admission 述語で決まり、複数が同時に適用可能になりうる。決定木は「配送が owed である」
      ことだけを first-match で決め、rail の選択は §3.1d に委ねる。

  # --- recovery phase の fall-through ---
  - id: G1
    when: {declared_pins: degraded}
    rail: none
    gap: GAP-2

  - id: G2
    when: {disposition: hibernated, worktree_identity: empty, declared_pins: absent}
    rail: none
    gap: GAP-3
```

### 3.1c terminal phase の rule 列

```yaml
terminal_rules:
  - id: T1
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
      - when: {}            # catch-all。上記 6 行が覆わない shape
        intent: []
        result: escalation
        reason: retire_intent_undefined_for_shape
        note: >
          T1 の `when` は `issue_state: closed` + `head_integrated: true` だけなので、
          `half_live` / `shell_residue` / `foreign_occupant` / `retired` / `superseded` /
          `hibernated + bound + both_live` 等も T1 に落ちる。それらに返す retire intent は
          **未定義**であり、自動選択せず escalation にする (round 5
          `finding_retireintenttotality`)。selection S2 は terminal を必ず primary にするので
          `no_phase` fallback には落ちない — したがって sub-selection 側で閉じる必要がある。
    note: "OVERLAP-3。declared_pins は preserve され validate されない。intent_by は catch-all で全域。"

  - id: T2
    when: {issue_state: open, park_basis: [dependency_park, early_hibernate]}
    rail: hibernate
    note: "T3 と交差しうる (§3.1f INT-1)。実装 supersede は issue の open/closed も park basis も見ない。"

  - id: T3
    when:
      disposition: active            # original lane。実装 original_identity_known は record 存在 + disposition==active + issue 一致の積
      successor_attested: true
      successor_same_issue: true
      original_idle: true
    rail: supersede
    note: >
      実装の gate は `original_identity_unknown` / `recovery_not_both_slots_live` /
      `recovery_not_attested` / `original_not_idle` の 4 件。`original_identity_unknown` は
      **original record 存在 ∧ lane_disposition == active ∧ issue 一致**の積なので、
      `disposition: active` を条件に含める (round 5 `finding_supersedeidentityadmission`)。"
```

### 3.1d 配送 rail (rail_set。実装 admission と policy 選択を分離する)

```yaml
delivery_rails:
  # 各 rail の `admission` は **実装が実際に読む軸だけ**を書く。他 rail の authority の
  # 有無を条件に足さない (round 4 `finding_deliverypartition`: gateway rail は
  # lifecycle_decision_journal を request にも preflight にも渡さず、出現数 0)。
  - id: D1
    rail: recover-pair-delivery
    admission:
      delivery_authority:
        recovery_anchor_authorization: valid_for_this_anchor
        zero_send_evidence: valid_for_this_action
        target_generation_pin: exact_live_generation
    reads_lifecycle_decision_journal: false

  - id: D2
    rail: recover-worker-delivery
    admission:
      delivery_authority:
        recovery_anchor_authorization: valid_for_this_anchor
        zero_send_evidence: valid_for_this_action
        target_generation_pin: exact_live_generation
        lifecycle_decision_journal: valid_for_this_lane
    reads_lifecycle_decision_journal: true

  - id: D3
    rail: rehydrate-fleet         # --execute の restore_dispatch
    admission: {dispatch_anchor: present}
    reads_lifecycle_decision_journal: false
    note: >
      `LaneDispatchFact.sendable` は `state == DISPATCH_OWED and bool(self.anchor_journal)` なので、
      `dispatch: owed` だけでは足りず **anchor journal の非空**も要る。anchor が空なら
      `dispatch_anchor_unresolved` で block する (round 5 `finding_deliverysendability`)。

policy_preference:
  # **policy であって実装の排他ではない。** 複数が適用可能なとき、どれを既定で提示するか
  # という運用上の順序に過ぎない。実装はどれも拒否しない。
  order: [D2, D1, D3]
  rationale: "authority が狭い (= 束縛が多い) ものを先に提示する。弱い authority で強い effect を得る事故を避けるため。"
  authority_is_exclusive: false
```

### 3.1e selection table

```yaml
selection:
  - id: S1
    when: {issue_state: open}
    primary: recovery
    fallback_if_primary_null: [hibernate, supersede]
    note: "open な issue を勝手に終端化しない。回復が無いときだけ park / 移管を採る。"

  - id: S2
    when: {issue_state: closed, head_integrated: true}
    primary: terminal
    fallback_if_primary_null: []
    note: "recovery rail は precursor (P1) としてのみ plan に載る。"

  - id: S3
    when: {issue_state: closed, head_integrated: false}
    result: escalation
    reason: head_not_integrated_on_closed_issue

  - id: S4
    when: {}
    result: escalation
    reason: no_phase_applies
```

### 3.1g precedence_basis (first-match 順序の正当化。閉語彙)

recovery phase の rule は 36 組が**同時 match しうる**。first-match は 1 つを選ぶので、
**なぜその順序なのか**を rule ごとに宣言する。`## 3.3` の検算 11 が、正当化も
`known_intersections` への列挙も無い交差を検出したら落ちる。

```yaml
precedence_basis_vocabulary:
  refused_by_later: "後続 rule の実装がこの shape を拒否するので、先行が唯一の有効経路。例 R8 → R5/R6/R7 (pending composer は pending_composer_input で zero-close refusal)。"
  least_effect_first: "双方 viable。effect budget の小さい方を先に採る。例 R2/R3a/R3b (metadata_only) → R5/R6/R7 (process 置換)。"
  precondition_of_later: "先行が後続の precondition を整える。両方が順に走りうる。例 R11 (worktree binding 修復) → R12/R13、R5/R6/R7 (受信者を生かす) → R14 (再配送)。"
  role_precedence: "同じ effect budget で role が違う。gateway は worker の dispatch 経路なので先に回復する。例 R5 → R6/R7。"
  diagnose_only_first: "実行経路を持たない診断 (status: diagnose_only) を先に提示する。例 R4。"

reporting_rule: >
  `precondition_of_later` / `role_precedence` / `least_effect_first` の交差では、**後続 rule も
  適用可能なまま**である。first-match が選ぶのは「今やる 1 つ」であって、後続を無効化しない。
  `evaluate` はそれらを `also_applicable` に載せる (`## 3.0` step 3)。`refused_by_later` の
  交差だけは後続が実装で拒否されるので `also_applicable` に載せない。
```

### 3.1f known_intersections (first-match が隠す共適用)

**同一 first-match 列の中で同時 match しうる rule の組は、必ずここに列挙する。**
`## 3.3` の検算 11 が、列挙されていない交差を検出したら落ちる。

```yaml
known_intersections:
  - id: INT-1
    phase: terminal
    rules: [T2, T3]
    disposition: escalation
    reason: hibernate_supersede_intersection
    detail: >
      open issue + park basis が成立し、同時に successor attested + same issue + original idle が
      成立する shape は実在する。実装 supersede の gate は identity / recovery pair live /
      recovery attested / original idle の 4 件のみで、**issue の open/closed も park basis も
      見ない** (`sublane_supersede.py` の block 定数全列挙 / `issue_closed` `park` の出現数 0)。
      first-match は常に T2 を選んで T3 を隠すが、hibernate (process release、所有権は不変) と
      supersede (所有権移管 + 旧 lane release) は **effect が違う authority decision** なので、
      決定木では自動選択せず escalation にする。判断は `## 5` の escalation 対象。

  - id: INT-3
    phase: terminal
    rules: [T1, T3]
    disposition: escalation
    reason: retire_supersede_intersection
    detail: >
      `issue_state: closed` かつ `head_integrated: true` の lane で、同時に successor attested +
      same issue + original idle が成立する shape も実在する。supersede の実装は issue の
      open/closed を見ないので、retire (終端化) と supersede (所有権移管 + 旧 lane release) の
      どちらも admit される。**「閉じた issue に後継へ渡す意味は無いはず」というのは意図であって
      実装の制約ではない** ので、doc 側で排他を創作せず escalation にする
      (round 3 / round 4 で 2 度、実装に無い条件を創作して排他を偽装した反省による)。

  - id: INT-2
    phase: recovery
    rules: [D1, D2, D3]
    disposition: co_applicable
    reason: delivery_authority_not_exclusive
    detail: >
      §3.1d のとおり 3 rail は実装上排他ではない。適用可能なものを全列挙し
      `policy_preference` の順に提示する。`## 3.5` OVERLAP-4 の実体。
```

### 3.2 effect-budget gap

```yaml
effect_budget_gaps:
  - id: EBG-1
    gap: GAP-4
    rule: R9b
    subject: "hibernated + bound + pins absent + server 復元で slot_verdict が recover_bad_generation になる pair"
    available_effect_budget: [replace_bad_slots_then_repair_pins]
    missing_effect_budget: session_preserving_metadata_only
    note: >
      R9b はこの shape の subject を持つので「レールが無い」のではない。無いのは
      「復元された provider session を close せずに pin だけ宣言する」effect budget。
      metadata-only の R9a は `slot_verdict: healthy_no_action` の pair しか扱えない。
      #15811 が ACTIVE 側に作った adopt-restored-pair の hibernated 版に相当する。
```

### 3.3 整合チェック (この doc 内で検算できる形にしてある)

1. **軸の閉包** — 全 rule / precursor / selection / delivery rail が参照する top-level key は、
   `any_axis` を除きすべて `pair_shape` に宣言された軸である。
2. **slot fact の閉包** — `all_slots` / `any_slot` / `any_slot_other` / `slot_health.<role>` の中の
   key は `slot_facts` の 14 軸か派生値 `slot_verdict` である。
3. **値の閉包** — 各条件の値は対応する軸の宣言値域に含まれる。
4. **到達可能性** — 同一 first-match 列で、ある rule の `when` が先行 rule の `when` の
   厳密な superset になっていないこと。
5. **`yaml` block の parse 可能性** — `yaml` と記した block はすべて parse できる data である。
   擬似コードは `text` block とする。
6. **gap の到達可能性** — `rail: none` の fall-through は recovery 列の末尾にあり、
   effect-budget gap は fall-through に置かない。
7. **selection の網羅** — catch-all (`when: {}`) の行が存在する。
8. **`slot_verdict` の全域性** — 各 step が単一結果を返し、最後に無条件の既定がある。
9. **id の一意性**。
10. **admission gate の対応** — `## 2.2` の表に挙げた各 rail の実装 gate が、対応する rule の
    `when` に条件として現れていること (note ではなく条件であること)。
11. **交差の明示** — 同一 first-match 列で同時 match しうる rule の組 (共有 key のすべてで
    値域が交差し、かつ `all_slots` / `any_slot` の値域が disjoint でない組) は、次のいずれかで
    閉じられていること: (a) `known_intersections` に列挙、(b) 先行 rule の `precedence_basis` が
    後続を `over` に含む。**どちらも無い交差は defect** (round 4 `finding_terminaloverlapclosure` /
    `finding_deliverypartition` の再発防止)。
12. **`precedence_basis` が閉語彙** — `## 3.1g` の 5 値以外を使っていないこと。
13. **admission 正本の照合** — `## 2.2` の各 `admission_source` を実装から読み、refusal /
    disposition 語彙の token 数が `admission_gate_count` と一致すること。実装が変われば
    ここが落ち、**doc の drift が可視化される**。
14. **符号化宣言の実在** — `routing_conditions_encoded` に挙げた軸が、対応する rule の
    `when` (delivery rail は `admission`) に**実際に条件として存在する**こと。note に書いただけの
    gate を「符号化した」と称せないようにする (round 5 `finding_railadmissionclosure` の再発防止)。

### 3.4 gap (回復方向にどのレールも subject にしていない shape)

**gap には 2 種類ある。混同しない。**

- **subject gap** — その shape を subject にする回復レールが存在しない (決定木の `rail: none`
  fall-through G1 / G2 に対応)。本表の GAP-1 / GAP-2 / GAP-3 / GAP-5。
- **effect-budget gap** — subject を持つレールは存在するが、effect budget の選択肢が 1 つしか
  なく保存側が選べない (`## 3.2` の EBG-1 に対応)。本表では **GAP-4**。決定木の fall-through
  には置けない (先行 rule が match するため到達不能になる)。

**主張の範囲**: 「回復方向にレールが無い」は **recovery phase (`## 3.1b`) についての主張**である。
terminal phase は `## 3.0` の評価アルゴリズムどおり**独立に評価される**ので、gap があっても
終端方向は到達しうる — 終端 rail 群は declared pin を **preserve するだけで validate しない**
(pin を検証するのは `repair-worktree-binding` のみ)。「回復できないが終端はできる」という
状態を gap と呼んでいる。

| id | shape | 現状 (回復方向) | 根拠 |
| --- | --- | --- | --- |
| **GAP-1** (決定木 R4、`status: diagnose_only`) | `active` + `declared_pins: resolvable` + `any_slot: {cwd: drifted}` (または attestation が non-green) | `recover-restored-pair` が唯一の subject だが、`RestoredPairPlan.generation_conditional_close_available` が**常に `False` を返す固定 property** であり、`blocked_reasons` に `generation_conditional_close_unavailable` が必ず入る。したがって `may_recover` は**構造的に常に False**。診断は在るが**回収経路は存在しない** | `domain/restored_pair_recovery.py` L163-234。CLI help も "Read-only until Herdr exposes an atomic generation-conditional close primitive" と明記。Herdr 0.8 / protocol 19 が close 変異に `pane_id` しか受けないことが根本原因。**実測 (2026-08-21, base `289343db`)**: 全 identity 充足 / `lifecycle_current=True` / `worktree_authority_current=True` / `allow_pending_composer_loss=True` / 片 slot だけ `cwd_matches=False` という最良 shape を構成しても `blocked_reasons == ('generation_conditional_close_unavailable',)` / `may_recover=False` |
| **GAP-2** (決定木 G1) | 任意 disposition + `declared_pins: degraded` (`unreadable` / `foreign_pin_role` / `mixed_pin_role_vocabulary` / `duplicate_pin_role` / `incomplete_pin_pair`) | **回復方向にどのレールも subject にしていない** (終端は T1 で到達しうる)。`adopt-restored-pair` は `declared_pins_present:<reason>` で明示拒否 (劣化 snapshot を上書きすると証拠が消えるため、これは意図的な正しい拒否)。`rebind-restored-pair` は exact 旧 pair 2 件を要求し `declared_slots_unresolved`。`repair-pins` は **empty** 限定。`repair-worktree-binding` は `declared_pins_fail_validation` / `declared_pins_are_not_canonically_encoded` で拒否。`read_declared_pin_pair` の全 consumer が非 OK を refusal として扱い、subject として消費する箇所は 0 | `src/` 全体で `PIN_PAIR_FOREIGN` / `_MIXED` / `_DUPLICATE` / `_INCOMPLETE` / `_UNREADABLE` を参照するのは定義元 `lane_pin_role.py` のみ (grep で確認) |
| **GAP-3** (決定木 G2) | `hibernated` + `released` + `worktree_identity: empty` + `declared_pins: absent` | **相互 precondition の deadlock。** `repair-worktree-binding` は pin を要求し `hibernated_record_missing_pins` で拒否。`repair-pins` と `converge-bound-pair` は **bound** row を要求 (`not_hibernated_released_bound_pins_empty`) するので、binding が空な限り走れない。**回復方向の出口が無く**、残る経路は `--migrate-hibernated-legacy` / `--retire-hibernated-unbound-live-zero` / `--reconcile-hibernated-live` の terminal 化のみ = この shape の lane は**終端しかできない** | `sublane_worktree_binding_repair.py` の `BLOCK_MISSING_PINS`、`sublane_hibernated_pin_repair.py` docstring ("hibernated / released **BOUND** ... `worktree_identity` present")、`hibernated_bound_pair_convergence.py` の `BLOCK_NOT_BOUND_SIGNATURE` |
| **GAP-4** (`## 3.2` EBG-1。**effect-budget gap**) | `hibernated` + `released` + `bound` + `declared_pins: absent` + server 復元により **`slot_verdict` が `recover_bad_generation`** になる pair (attestation が `restore_stale` / `stale` / `absent`) | **レールは在る。無いのは effect budget の選択肢。** 決定木 R9b (`converge-bound-pair`) がこの shape の subject を持つので「どのレールも扱えない」ではない。無いのは「復元された provider session を close せずに pin だけ宣言する」= session-preserving な metadata-only 予算である。metadata-only の `repair-pins` (R9a) は `slot_verdict: healthy_no_action` (= `attestation: live_joined` かつ cwd/locator 一致) の pair しか扱えず、server 復元で attestation を失った pair には適用できない。結果としてこの shape の唯一の前進手段が `converge-bound-pair` の破壊的置換になる。#15811 が ACTIVE 側に作った `adopt-restored-pair` の hibernated 版に相当する | `sublane_hibernated_pin_repair.py` docstring (metadata-only 宣言 + GREEN 条件) / `domain/sublane_hibernated_live_reconcile.py` の `STATE_GREEN` / `hibernated_bound_pair_convergence.py` の `APPROVAL_EFFECT = "replace_bad_pair_then_repair_pins"` と help ("replace the exact **stale/unattested** pair") |
| **GAP-5** (決定木外) | `audit-failure-terminal` が記録した監査失敗 lane の terminal retire | 記録は書けるが**何も authorize しない**。`coordinator_receipt_authority_unresolvable` で #15195 待ち。監査失敗 lane の終端は現在 owner 手動 | `cli_audit_failure_terminal_decision.py` help |

GAP-1 と GAP-5 は「レール乱立の副作用」ではなく **外部依存 (Herdr API / #15195) による既知残余**であり、
統合では解消しない。**GAP-2 / GAP-3 / GAP-4 は集合設計の欠落**であり、いずれも「個々のレールは
正しく fail-closed しているのに、回復方向の集合として出口が無い」という #15811 と同型の形をしている。
3 件の性質は異なるので区別して読むこと。

- **GAP-2** は subject の**不在** (劣化 pin を扱う回復レールが 1 本も無い)。
- **GAP-3** は**相互 precondition の deadlock** (2 本のレールが互いの前提を要求し合う)。#15811 が
  埋めた class に最も近い。
- **GAP-4** は種別が違う — **effect-budget gap** である。subject を持つレール (R9b) は在るが
  effect budget が破壊的置換の 1 種類しかない。したがって埋め方も「新レール」ではなく
  「既存 metadata-only 経路 (`repair-pins`) の subject を広げるか、session-preserving な
  予算をもつ subset を足すか」という形になる。**決定木の fall-through では表現できない**
  (R9b が先に match するため) ので、`## 3.2` の `effect_budget_gaps` として別建てにしている。

### 3.5 overlap (同一 shape を複数レールが扱う)

**overlap の定義と、その厳密な適用**: ここで overlap と呼ぶのは「`## 2` の pair shape ベクトルが
**同一の値**でありながら複数レールが subject を主張する」場合に限る。**durable row の signature
(`disposition` / `worktree_identity` / `declared_pins` / `process_release`) だけが一致していて
live 側の `slot_health` で排他になっているものは overlap ではなく split** であり、下表では
`partial (durable half のみ)` と明記して区別する。この区別は装飾ではない — 統合候補の
妥当性が変わる (真の overlap は 1 本に畳める可能性があるが、split を畳むと排他条件を
parameter 化することになり、別種の取引になる)。

種別は 3 値とする。

- **true** — pair shape ベクトルが同一値のとき、常に複数レールが適用可能。
- **conditional true** — ある軸の値によって「共適用」と「排他」が切り替わる。条件を明記する。
- **partial** — 共通しているのは shape ではなく machinery / 契約 / durable half だけで、
  pair shape 上は常に排他。

| id | 重複するレール | 種別 | 重複の実体 | 現時点で分かれている理由 (docstring 由来) |
| --- | --- | --- | --- | --- |
| **OVERLAP-1** | `recover-stale` (R7) / `recover-gateway` (R5) / `refresh-worker` (R6) | **true** (`## 3.1g` の `role_precedence` 交差) | pair shape は gateway と worker の `slot_health` を**同時に**持つので、gateway が live-failed かつ worker も live-failed なら **R5 と R6 が同一ベクトルで同時 match** し、gateway が live-failed かつ worker が shell-residue なら **R5 と R7 が同時 match** する。実際 R5 の `precedence_basis` 自身が R6 / R7 に対する `role_precedence` を宣言している。first-match は gateway を先に選ぶが、**後続 rule は適用可能なまま**で `also_applicable` に載る (`## 3.1g` reporting_rule)。3 本は #13806 tranche A/B の同一 actuation を共有し、`worker_turn_recovery` は `gateway_turn_recovery.classify_gateway_turn` を逐語再利用する | 保護対象の集合が互いに反転している (gateway 保護 / worker 保護 / 両方保護)。staleness 述語が異なる (`liveness: vanished` ではなく `shell_residue` / `productivity: turn_ended_unproductive`)。#14661 j#92369 が「vanished worker と live-but-unproductive worker は別の事実で別の admission」と設計制約として固定 |
| **OVERLAP-2** | `repair-pins` / `converge-bound-pair` | **partial** (durable half のみ。live 側で排他) | 一致するのは **durable row signature** だけ (`hibernated` + `released` + `bound` + pins empty = `not_hibernated_released_bound_pins_empty`)。**live pair 状態では排他**である: `repair-pins` が pin を書けるのは `decide_pair_reconcile` が **GREEN** を返す pair (present / unique / live / idle-or-turn-ended / composer-settled / generation-bound attested) のときだけで、`converge-bound-pair` の subject は逆に「その GREEN を満たさない **stale / unattested** な pair」(`APPROVAL_EFFECT = "replace_bad_pair_then_repair_pins"`)。決定木では R9a / R9b として分離した | actuation 予算が違う (metadata-only vs process 置換)。#13879 は #13847 の precondition を弱めないことを明示目的にしている |
| **OVERLAP-3** | `retire` の 6 intent + 既定 | **partial** (terminal CAS 契約のみ。shape は分割) | 7 経路すべてが「terminal disposition への CAS」を共有するが、`intent_by` 表のとおり `(disposition × worktree_identity × live_pair)` で**互いに排他な分割**になっている。畳めるのは契約であって shape ではない | 各 intent が異なる liveness authority を持つ。#14242 は「ACTIVE row は `process_release == not_requested` なので live-inventory 読取が唯一の liveness authority」であり #13845 より要求が高い、と明記。#14499 は「operator が 5 intent を 1 語彙で読めるよう #14242 を意図的に mirror した」と記録 |
| **OVERLAP-4** | `recover-pair-delivery` (D1) / `recover-worker-delivery` (D2) / `rehydrate-fleet --execute` の `restore_dispatch` (D3) | **true** (`## 3.1f` INT-2) | **3 rail は実装上互いに排他ではない。** D1 は `RecoveryDeliveryAuthorization` + `ZeroSendEvidence` + exact target 世代 pin を要求し、D2 はそれに `lifecycle_decision_journal` の exact join を追加し、D3 は `dispatch.sendable` **のみ**を読む。**gateway rail (D1) は lifecycle decision journal を request にも preflight にも渡さない** (`sublane_recover_pair_delivery.py` / `domain/recovery_anchor_delivery.py` での出現数 0)。したがって各 rail の authority が成立する shape では複数が同時に適用可能になる。決定木では R14 が `rail_set: delivery_rails` を返し、`## 3.1d` が適用可能な rail を全列挙、`policy_preference` は **policy であって排他ではない** ことを `authority_is_exclusive: false` で明示する | 経路 (gateway 経由 / worker 直送 / fleet 単位)、要求 authority、effect 契約が異なる。D1 / D2 は `recovery_effect_contract` の applied-effect / unresolved-fate 契約を共有する |
| **OVERLAP-5** | `reboot-audit` / `rehydrate-fleet` (plan) | **true** (同一 shape) | 決定木 P0 (precursor) が両方を挙げる。同一の per-lane joined facts に対する 2 つ目の per-lane 決定 | `fleet_rehydrate` docstring が明示: #14499 は「この lane はどの disposition へ収束すべきか」、#15745 は「この lane はどの未配送 action を負っているか」で**問いが違う**。#14499 の「lane ごとに違う答え」性質を潰さないため別 planner にした |
| **OVERLAP-6** | `quarantine` / `prepare-bound-pair` / `refresh-worker` | **partial** (`pending_composer` 軸のみ。disposition で排他) | 3 本が「pending composer が前進を塞ぐ」状態を扱うが、決定木では R8 (`active`) / R10 (`hibernated` + bound + pins absent) / R6 (worker が live-unproductive) と前提が分かれる | 承認 gate が別 (`quarantine` の 5 token / `bound_pair_composer_discard_approval` / `worker_refresh_owner_approval`) |
| **OVERLAP-7** | `adopt-restored-pair` / `rebind-restored-pair` | **partial** (`declared_pins` で排他) | 共有するのは「ACTIVE lane の server-restored pair で pin snapshot が現実と合わない」という**問題設定**であり、`declared_pins` の値 (`absent` vs `resolvable`) で排他 (R2 / R3)。両者は既に status 語彙と `slot_reason` を共有している | pin snapshot が absent か stale かで CAS が別物 (empty-only backfill vs exact-2 件 replace)。adopt は rebind より**厳しい** proof chain を要求する (declared pin という照合先が無いため) |
| **OVERLAP-8** | `hibernate` (T2) / `supersede` (T3) | **true** (`## 3.1f` INT-1、disposition=escalation) | open issue + park basis が成立し、同時に successor attested + same issue + original idle が成立する shape は実在する。**supersede の実装 gate は `original_identity_unknown` / `recovery_not_both_slots_live` / `recovery_not_attested` / `original_not_idle` の 4 件のみで、issue の open/closed も park basis も見ない** (`sublane_supersede.py` の block 定数全列挙、`issue_closed` / `park` の出現数 0)。terminal first-match は常に T2 を選んで T3 を隠すが、hibernate (process release、所有権不変) と supersede (所有権移管 + 旧 lane release) は **effect が違う authority decision** なので自動選択しない | 実装が排他にしていない。どちらを採るかは owner / coordinator の判断 (`## 5` escalation) |
| **OVERLAP-9** | `retire` (T1) / `supersede` (T3) | **true** (`## 3.1f` INT-3、disposition=escalation) | `closed` + `head_integrated` の lane で successor attested + same issue + original idle が成立する shape も同様に両方 admit される。「閉じた issue に後継へ渡す意味は無いはず」は**意図であって実装の制約ではない**ので、doc 側で排他を創作せず escalation にした | 同上 |

**この分類から出る所見**: 9 件のうち **true overlap は 5 件 (OVERLAP-1 / 4 / 5 / 8 / 9)**、
partial が 4 件 (OVERLAP-2 / 3 / 6 / 7)。**true の 5 件はいずれも「実装が排他にしていない」
class** であり、うち 4 件 (1 / 4 / 8 / 9) は round 3-5 の review で発見された。
OVERLAP-1 は round 4 で `precedence_basis` を導入した際に**overlap 表を追随させ忘れた**もので、
`role_precedence` を宣言しておきながら同じ表で「shape 上は常に排他」と書く自己矛盾だった
(round 5 `finding_overlaponeclassification`)。

判定は 3 度訂正している。OVERLAP-4 は true → partial (round 2) → conditional true (round 3) →
**true** (round 4)。訂正の原因は毎回同じで、**実装が排他にしていないものを doc 側で排他だと
思い込み、そのための条件を創作した**こと。round 4 でその再発防止として
`## 3.1f known_intersections` と `## 3.1g precedence_basis`、および `## 3.3` の検算 11 を導入した。

したがって「レールが乱立している」という問題の実体は 2 つある。**(a) 近傍の shape ごとに
1 本ずつレールが生えた結果、集合としての被覆に穴が空いている** (= `## 3.4` の gap)、
**(b) 実装が排他にしていない rail の組が複数あり、どれを採るかの決定規則が
どこにも書かれていなかった** (= 本節の true overlap 4 件)。統合の主目的を「重複削除」に
置くと GAP-2 / GAP-3 / GAP-4 は閉じない。

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

**前提の訂正**: OVERLAP-2 は **true overlap ではなく split** である (`## 3.5`)。2 本が一致するのは
durable row signature だけで、live pair 状態 (`decide_pair_reconcile` が GREEN か否か) で排他する。
したがって本候補は「重複を削る統合」ではなく、**1 つの subject 軸 (live pair の GREEN 性) で
分岐する 1 rail に畳めるか**という問いである。畳むと、GREEN のときは metadata-only、非 GREEN の
ときは破壊的置換、という**effect budget が入力で変わる rail** が生まれる点が本質的な取引になる。

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

**前提の訂正 (3 度目。round 4 review `finding_deliverypartition`)**: OVERLAP-4 は **true overlap**
である。D1 / D2 / D3 は実装上互いに排他ではなく、各 rail の authority が成立する shape では
複数が同時に適用可能になる。**gateway rail (D1) は lifecycle decision journal を読まない**ので、
round 3 で自分が R14a に置いた `lifecycle_decision_journal: [absent, mismatched]` は
実装に無い条件の創作だった。

したがって本候補の問いは「重複を削る統合」ではなく次の 2 つ。

1. **どれを採るかの決定規則をどこに置くか。** 現状 `## 3.1d` の `policy_preference`
   (`order: [D2, D1, D3]` / `authority_is_exclusive: false`) は **doc が提案する運用順序**で
   あって実装の制約ではない。実装側に持たせるなら authority 階梯を型にする必要がある。
2. **弱い authority が強い effect を得ない**ことをどう保つか。D3 (`dispatch.sendable` のみ) が
   D1 / D2 と同じ再配送を行える現状は、owner 承認付き経路と承認なし経路が同一 shape に
   共適用できることを意味する。これを仕様として認めるかは authority policy の判断であり
   `## 5` の escalation 対象。

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
4. **GAP-4 (`repair-pins` の subject が GREEN pair に限られる)** の扱い: hibernated + bound +
   pins absent な lane の **stale / unattested な restored pair** を、session を保存したまま
   採用する経路を用意するか。選択肢は (a) `repair-pins` の subject を広げる、(b) #15811 の
   `adopt-restored-pair` に相当する hibernated 専用 subset を足す、(c) 用意せず「hibernated の
   復元 session は `converge-bound-pair` の破壊的置換でしか回収できない」ことを明示決定する。
   **`repair-pins` という metadata-only 経路自体は既に存在する**ので、これは経路の新設ではなく
   subject 範囲の決定である。
5. **C1 の統合可否**: 型で不能にしている保護反転を parameter 化する取引を受け入れるか。
6. **C2 の統合可否**: effect budget が入力 (live pair の GREEN 性) で変わる rail を許容するか。
   現状は「metadata-only の rail」と「破壊的 rail」が別 command である点が operator への
   安全表示になっている。
7. **C3 の統合可否**: intent flag の operator 可読性 (#14499 が意図的に mirror した性質) を
   宣言表に畳んだあとも維持できるか。
8. **C6 が ADR-0001 の反転に当たるか**の判定。
9. **統合の主目的の確認**: `## 3.5` の分類では **true overlap 4 件 / partial 5 件**で、
   true の 4 件はいずれも「実装が排他にしていない」class だった。統合の目的を「重複削除」に
   置くと GAP-2 / GAP-3 / GAP-4 は 1 つも閉じない。Phase 2 の主目的を「被覆の穴 (subject gap +
   effect-budget gap) を閉じる」と「排他でない rail の決定規則を置く」の 2 本に置き直してよいか。
10. **OVERLAP-4 の共適用を仕様として認めるか**: `restore_dispatch` が他 rail の action-scoped
   authority を参照せず共適用しうるのは、実装の意図なのか未整理なのか。owner 承認付き経路と
   承認なし経路が同じ shape に同時適用できる状態を許容するかは authority policy の判断であり、
   本 doc は判定しない。
11. **INT-1 (`hibernate` vs `supersede`) の裁定**: open issue + park basis + attested successor の
    shape でどちらを採るか。実装はどちらも admit する。hibernate は所有権不変で process を
    release、supersede は所有権を後継へ移す — effect が違うので既定を決めるか、常に
    escalation のままにするかを決めてほしい。
12. **INT-3 (`retire` vs `supersede`) の裁定**: closed + integrated の lane に attested successor が
    いる shape。supersede の実装が issue state を見ないのは意図か未整理か。
13. **`## 3.3` の整合チェックを repo に置くか** (本 US では判断しない): 14 項目は現在
    「reviewer / implementer が手元で再実装して回せる形」で記述されているだけで、継続的に
    実行される guard は存在しない。置くなら `tests/` 配下の docs-parity guard が自然だが、
    それは新規 test の追加であり IR (#15841 j#109727) の scope fence「コード・テストの挙動は
    一切変えない (分析 + doc のみ)」の解釈判断を要する。本 doc は現時点で
    **「検算できる形にしてある」ことまでを主張し、「継続的に検算される」とは主張しない**。
14. **決定木の fidelity 目標の裁定 (round 5 で escalate。判断待ち)**: 本 doc の決定木は
    (a) **routing の道具** — 候補 rail を選ぶまでを担い、admit は各 rail の preflight に委ねる、
    (b) **admission の完全写像** — 実装の ordered gate をすべて `when` に符号化する、
    のどちらを Phase 1 の到達目標とするか。IR (#15841 j#109727) と issue description は
    「機械可読に**近い**決定木」= (a) 寄りの表現だが、review round 2-5 の material finding 20 件は
    ほぼすべて (b) の基準で提起されている。(b) を厳密に満たすと決定木は実装の ordered gate を
    再実装することになり、**正本が二重化して drift 源になる** — 本 doc が Phase 1 で避けようと
    している形そのものである。`## 2.2` はこの緊張に対する暫定解 (routing に限定し、admission の
    正本所在と符号化の実在だけを機械保証する) だが、目標そのものの裁定は scope 判断であり
    implementer が決めるべきではない。**裁定が出るまでは厳しい側 (b) を目標として作業を続ける**
    (fail 側に倒すのが規約であるため)。
15. Phase 2 の受け入れ条件を「本 doc の C1-C5 に列挙した test が 1 本も緑を失わない」で
    固定してよいか。

## 参照

- ADR-0011 (3階層の責務分担) / ADR-0001 (owner 決定の ADR 記録)
- `vibes/docs/logics/managed-state-model.md` (lifecycle 正本)
- `vibes/docs/specs/herdr-native-identity.md` / `vibes/docs/specs/route-identity-ledger.md`
- `src/mozyo_bridge/core/state/lane_pin_role.py` (`PIN_PAIR_*` 語彙の正本)
- `src/mozyo_bridge/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/`
  (全レールの application / domain)
