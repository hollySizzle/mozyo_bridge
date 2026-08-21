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
前者は block、後者は**その条件を空虚に満たす** (`## 2.2`)。

**この軸集合は `## 3` の全 rule が参照する観測の閉集合である** — rule はここに宣言されていない
key を参照しない (`## 3.3` の整合チェックを参照)。

```yaml
pair_shape:

  # --- A. durable lifecycle row 由来 ---
  disposition:        [active, hibernated, retired, superseded, unknown]
  worktree_identity:  [bound, empty, unknown]
  declared_pins:      [resolvable, absent, degraded, unknown]   # degraded = unreadable|foreign_pin_role|mixed_pin_role_vocabulary|duplicate_pin_role|incomplete_pin_pair
  process_release:    [not_requested, in_flight, released, unknown]

  # --- B. live inventory 由来。slot_health は role ごとに直交事実の直積を持つ ---
  live_pair:          [both_live, half_live, zero_live_positive, shell_residue, foreign_occupant, unknown]

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
    worker: *slot_facts

  # --- C. Redmine / git 由来 ---
  issue_state:        [open, closed, unknown]
  head_integrated:    [true, false, unknown]

  # --- D. durable record 由来の判定軸 ---
  stale_signal:       [positive, negative, unknown]   # #13518 shell-residue の positive 判定 (`recover-stale` の is_stale 前提)
  dispatch:           [owed, delivered, uncertain, unreadable, attribution_unknown, not_applicable]
  park_basis:         [dependency_park, early_hibernate, absent, unknown]
  resume_gates:       [green, blocked, unknown]       # `resume` の release-settled / issue-reowned / generation fence の総合
  successor_attested: [true, false, unknown]
  successor_same_issue: [true, false, unknown]
  original_idle:      [true, false, unknown]

  # --- E. 配送 authority。**action-scoped な valid-for 述語**であって present/absent ではない ---
  # 実装の authorization は (issue, lane, anchor_journal, retry_of_action_sha256) に束縛された
  # 「この anchor / この action に対して有効か」であり、汎用の有無では rail を区別できない
  # (round 3 review j#109757 `finding_deliverypartition`)。
  delivery_authority:
    recovery_anchor_authorization: [valid_for_this_anchor, mismatched, absent, unknown]
    zero_send_evidence:            [valid_for_this_action, mismatched, absent, unknown]
    target_generation_pin:         [exact_live_generation, stale, absent, unknown]
    lifecycle_decision_journal:    [valid_for_this_lane, mismatched, absent, unknown]
```

軸の出所:

- `declared_pins` の値語彙は `core/state/lane_pin_role.py` の `PIN_PAIR_*` 正本。
- `attestation` の `live_joined` / `restore_stale` の区別は `sublane_restored_pair_adopt_live.py` の
  `attested` (live-joined) と `attestation_restore_stale` (#15769 の restore-stale signature) に対応する。
  **`absent` は adopt / rebind いずれでも refusal 側**であり admit 値ではない。
- `launch_generation_row` / `participant_lineage` は `adopt-restored-pair` の proof chain を表す軸。
  同 rail は「usable な attested row が無い」を `ADOPT_SLOT_GENERATION_ABSENT` の **hard refusal**
  にする (実装コメント: "This rail has no declared pin to fall back on, so the missing server-owned
  row is a hard refusal")。`rebind-restored-pair` は pre-#15769 の attestation-only fall-through を
  まだ許すので、この 2 軸は rail ごとに要求が違う。
- `productivity: turn_ended_unproductive` (process は live) と `liveness: vanished` (process が消えた)
  は**別軸の別事実**。混同しない (#14661 j#92369)。
- `dispatch` の値語彙は `domain/fleet_rehydrate.py` の `DISPATCH_*` 正本。
- `park_basis` の 2 値は `sublane hibernate` の affirmative park basis (#13967 item 1)。
- `delivery_authority` の 4 軸は `RecoveryDeliveryAuthorization` / `RecoveryDeliveryZeroSendEvidence` /
  `RecoveryAnchorDeliveryRequest` / `RecoveredWorkerDeliveryRequest` の field から導いた。

### 2.1 派生値: `slot_verdict`

決定木の一部の rule は raw な直積ではなく**この派生値**を読む。定義は
`decide_slot_recovery` (`domain/hibernated_pair_recovery.py`) の順序評価を写したものであり、
**正本は実装側。本 doc は再宣言ではなく対応表である。**

```text
slot_verdict(slot):        # 上から最初に成立したもの
  1. liveness == vanished:
       generation_rank != newer  -> recover_bad_generation      # short-circuit。以降の gate は評価されない
       else                      -> preserve_newer_generation
  2. membership in [ambiguous, unknown]        -> preserve_ambiguous
  3. membership == foreign                     -> preserve_foreign
  4. generation_rank == newer                  -> preserve_newer_generation
  5. productivity == productive                -> preserve_productive
  6. composer == pending                       -> preserve_pending_composer
  7. worktree == unreadable                    -> preserve_worktree_unreadable
  8. attestation == live_joined
     and cwd == matches and locator == pinned_match  -> healthy_no_action
  9. else                                      -> recover_bad_generation | preserve_ambiguous
```

`slot_verdict` は rule の条件 key として使ってよい (`all_slots` / `any_slot` / `slot_health.<role>`
のいずれでも)。値域は
`[recover_bad_generation, healthy_no_action, preserve_ambiguous, preserve_foreign,
preserve_newer_generation, preserve_productive, preserve_pending_composer,
preserve_worktree_unreadable]`。

### 2.2 `not_applicable` の規約 (short-circuit された gate)

実装は `liveness: vanished` の slot について **membership / attestation / launch_generation_row /
participant_lineage / productivity / composer / cwd / locator / worktree を評価しない**
(`decide_slot_recovery` の step 1 が return し、live 側の観測も
`SlotRecoveryObservation(slot_absent=True, generation_not_newer=...)` **だけ**を設定する)。

- そのとき上記 9 軸の値は `not_applicable` とする。**`unknown` にしてはならない** — `unknown` は
  P0 の診断 block へ落ちるので、実装が正常に admit する partial-close replay が block と誤符号化される。
- `all_slots` / `any_slot` / `slot_health.<role>` の条件照合において、**`not_applicable` はその条件を
  空虚に満たす** (skip)。`any_slot` の存在量化は `not_applicable` を「成立」に数えない。
- `liveness` と `generation_rank` は vanished slot でも観測されるので `not_applicable` を取らない。

## 3. 決定木

### 3.0 評価アルゴリズム (実行意味論)

**単一 first-match ではない。** `evaluate` は **ordered plan または escalation** を返す。

```text
evaluate(shape) -> Plan | Escalation

  # 1. precursor: 宣言順に評価し、match したものを steps へ (閉集合。§3.1a)
  steps = [p for p in precursors in declared order if match(p.when)]
  if any(p.blocks_further_evaluation for p in steps):
      return Escalation(reason=p.id, steps=steps)      # 再観測してからやり直す

  # 2. phase ごとに独立に first-match (§3.1b / §3.1c)
  recovery = first_match(rules where phase == recovery)     # 無ければ null
  terminal = first_match(rules where phase == terminal)     # 無ければ null

  # 3. co-applicable rail は first-match の precedence に参加せず、独立に収集 (§3.1d)
  also = [r for r in co_applicable_rules if match(r.when)]

  # 4. selection table (§3.1e) を上から評価し、最初に match した行で決める
  sel = first_match(selection)
  if sel.result == escalation:
      return Escalation(reason=sel.reason, steps=steps, recovery=recovery, terminal=terminal)

  primary = recovery if sel.primary == "recovery" else terminal
  if primary is null:
      primary = fallback(sel)        # 各行が admit する閉集合から。無ければ escalation
  if primary is null:
      return Escalation(reason="no_applicable_rail", steps=steps)

  # 5. plan = precursor steps (宣言順) → primary rail → also (参考提示)
  return Plan(steps=steps, selected=primary, also_applicable=also)
```

**確定していること (前 round の散文を data 化した点)**:

- precursor は `## 3.1a` の**閉集合**であり、他の rail を「前段」に使うことはできない。
- どちらの phase を primary にするかは `## 3.1e` の selection table が決める。散文ではない。
- `also_applicable` は「同じ shape に別 authority で適用しうる rail」を**参考として並置**する枠で、
  primary を置き換えない。これが無いと `## 3.5` OVERLAP-4 の実態 (条件付き共適用) を符号化できない。
- どの phase にも該当しない shape、および `closed` かつ `head_integrated: false` は
  **escalation であって自動選択しない**。

**量化子**: `all_slots` は gateway と worker の両方、`any_slot` は少なくとも一方。
`slot_health.<role>` は role 名指し。`not_applicable` の扱いは `## 2.2`。

**rule 順序の規約 (到達可能性)**: 同一 phase 内では**狭い条件を先に置く**。ある rule の `when` が
先行 rule の `when` の**厳密な superset** になっていると、その rule は first-match で到達不能になる。
`## 3.3` でこれを検算する。

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
    when: {live_pair: shell_residue}
    step: close-residue
    blocks_further_evaluation: false
    rationale: >
      residue を閉じるまで live-zero 読取が正直にならない。recovery / terminal の
      どちらの前段にもなるので precursor に置く。close-residue は lifecycle row を
      書かないので、これ自体は終端でも回復でもない。
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
        attestation: [live_joined, restore_stale]   # absent は ADOPT_SLOT_GENERATION_ABSENT / unattested_slot で hard refusal
        launch_generation_row: attested_bound       # 必須。fall-through 無し
        participant_lineage: joined                 # 必須
    rail: adopt-restored-pair
    effect_budget: metadata_only
    note: >
      #15811 が埋めた class。pin 不在は create path の正常 shape であって record 劣化ではない。
      adopt に single-slot mode は無いので all_slots で要求する。
      rebind (R3) より **厳しい** proof chain を要求する — 照合先の declared pin が無いため。

  - id: R3
    when:
      disposition: active
      declared_pins: resolvable
      all_slots: {liveness: live, membership: this_pair}
      any_slot: {locator: drifted}
    rail: rebind-restored-pair
    effect_budget: metadata_only
    note: >
      少なくとも 1 slot が実際に drift していること (`locator_not_drifted` /
      `declared_locator_still_live` で refuse)。R2 と違い `launch_generation_row` は
      必須でない — pre-#15769 の attestation-only fall-through を許す rail であるため。

  - id: R4
    when:
      disposition: active
      declared_pins: resolvable
      all_slots: {liveness: live, membership: this_pair}
      any_slot: {cwd: drifted}
    rail: recover-restored-pair
    status: diagnose_only         # GAP-1: 実行経路が構造的に存在しない
    effect_budget: none

  - id: R8
    when:
      disposition: active
      any_slot: {composer: pending}
    rail: quarantine
    requires: quarantine-inspect  # approval token の取得元
    note: >
      **pending composer を扱う唯一の recovery rail なので R5 / R6 / R7 より前に置く。**
      R5 / R6 は実装側で `pending_composer_input` の zero-close refusal になるため、
      pending を先に quarantine へ吸わないと決定木が拒否 shape を推奨してしまう
      (round 3 review `finding_pendingcomposerorder`)。

  - id: R5
    when:
      disposition: active
      slot_health.gateway:
        {liveness: live, membership: this_pair, generation_rank: current,
         productivity: turn_ended_unproductive, composer: settled}
    rail: recover-gateway
    protects: [worker, default_coordinator, foreign]
    note: "gate 対応: identity_unknown / non_gateway_protected / wrong_issue_lane / stale_generation / gateway_not_settled / pending_composer_input。"

  - id: R6
    when:
      disposition: active
      slot_health.worker:
        {liveness: live, membership: this_pair, generation_rank: current,
         productivity: turn_ended_unproductive, composer: settled, worktree: readable}
    rail: refresh-worker
    protects: [gateway, default_coordinator, foreign]
    note: >
      gate 対応: identity_unknown / gateway_or_foreign_protected / wrong_issue_lane /
      stale_generation / worker_not_settled / **pending_composer_input** /
      dirty_state_unreadable。`worktree: readable` は「dirty を保存するために読めること」で
      あって clean であることではない。

  - id: R7
    when:
      disposition: active
      slot_health.worker: {liveness: vanished, membership: this_pair, generation_rank: current}
      stale_signal: positive
    rail: recover-stale
    protects: [gateway, default_coordinator, foreign]
    note: >
      `liveness: vanished` は R6 の `productivity: turn_ended_unproductive` と別軸の別事実。
      `recover-stale` は後者を `not_stale` で拒否する (#14661 j#92369)。vanished slot では
      membership 以外の観測が `not_applicable` になりうる (`## 2.2`)。

  - id: R9a
    when:
      disposition: hibernated
      worktree_identity: bound
      declared_pins: absent
      process_release: released
      all_slots: {slot_verdict: healthy_no_action}
    rail: repair-pins
    effect_budget: metadata_only
    note: "`decide_pair_reconcile` が GREEN を返す pair だけ。close/launch/resume/send を一切しない。"

  - id: R9b
    when:
      disposition: hibernated
      worktree_identity: bound
      declared_pins: absent
      process_release: released
      all_slots: {slot_verdict: [recover_bad_generation, healthy_no_action]}
      any_slot:  {slot_verdict: recover_bad_generation}
    rail: converge-bound-pair
    effect_budget: replace_bad_slots_then_repair_pins
    note: >
      実装 (`sublane_hibernated_bound_pair_convergence` L233-258) の admit 条件と 1:1:
      全 slot が `SLOT_RECOVER` か `SLOT_HEALTHY`、かつ `pins_exact and all(SLOT_HEALTHY)` の
      ときだけ `ALREADY_CONVERGED` = **all healthy 以外は action 対象**。
      片側 healthy + 片側 bad も、**片側 vanished (partial close replay) + 片側 healthy** も
      正規の subject になる — vanished slot は `slot_verdict` で `recover_bad_generation` に
      落ちるため (`## 2.1` step 1)。healthy な slot は保存され bad な slot だけが置換される。

  - id: R10
    when:
      disposition: hibernated
      worktree_identity: bound
      declared_pins: absent
      any_slot: {slot_verdict: preserve_pending_composer}
    rail: prepare-bound-pair
    then: converge-bound-pair
    note: >
      convergence は pending composer を必ず保存する (`pair_contains_preserved_slot`) ので、
      discard は別 gate の前段が要る。R9b より**前**に置く必要はない — R9b は
      `slot_verdict` が `preserve_pending_composer` の slot を含む shape に match しないため
      両者は排他である。

  - id: R11
    when:
      disposition: hibernated
      worktree_identity: empty
      declared_pins: resolvable
      process_release: released
    rail: repair-worktree-binding
    effect_budget: metadata_only

  - id: R12
    when:
      disposition: hibernated
      declared_pins: resolvable
      any_slot: {slot_verdict: recover_bad_generation}
    rail: recover-pair
    effect_budget: replace_bad_slots_then_redispatch

  - id: R13
    when:
      disposition: hibernated
      live_pair: both_live
      issue_state: open
      resume_gates: green
    rail: resume
    effect_budget: disposition_cas_only

  # --- 配送: 狭い順に置く (R14b ⊂ R14a なので R14b が先) ---
  - id: R14b
    when:
      disposition: active
      issue_state: open
      dispatch: owed
      delivery_authority:
        recovery_anchor_authorization: valid_for_this_anchor
        zero_send_evidence: valid_for_this_action
        target_generation_pin: exact_live_generation
        lifecycle_decision_journal: valid_for_this_lane
    rail: recover-worker-delivery
    note: "worker 直送。3 者で最も狭い。`lifecycle_decision_journal` を追加で束縛する。"

  - id: R14a
    when:
      disposition: active
      issue_state: open
      dispatch: owed
      delivery_authority:
        recovery_anchor_authorization: valid_for_this_anchor
        zero_send_evidence: valid_for_this_action
        target_generation_pin: exact_live_generation
        lifecycle_decision_journal: [absent, mismatched]   # 明示的な非適用条件 (R14b との排他を順序に依存させない)
    rail: recover-pair-delivery
    note: "gateway 経由。R14b の要件から lifecycle decision journal を除いたもの。"

  # --- recovery phase の fall-through: 回復方向に subject を持つレールが無い shape ---
  - id: G1
    when: {declared_pins: degraded}
    rail: none
    gap: GAP-2
    note: "劣化 snapshot を subject にする回復レールが 0。terminal phase は独立に評価されるので抑止しない。"

  - id: G2
    when: {disposition: hibernated, worktree_identity: empty, declared_pins: absent}
    rail: none
    gap: GAP-3
    note: "repair-worktree-binding は pin を要求し、repair-pins / converge-bound-pair は bound を要求する相互 deadlock。"
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
    note: "OVERLAP-3。declared_pins は preserve され validate されない。"

  - id: T2
    when: {issue_state: open, park_basis: [dependency_park, early_hibernate]}
    rail: hibernate

  - id: T3
    when: {successor_attested: true, successor_same_issue: true, original_idle: true}
    rail: supersede
```

### 3.1d co-applicable rail (first-match の precedence に参加しない)

```yaml
co_applicable_rules:
  - id: R14c
    when: {disposition: active, issue_state: open, dispatch: owed}
    rail: rehydrate-fleet         # --execute の restore_dispatch
    note: >
      **R14a / R14b と排他ではない。** 実装は `if facts.dispatch.sendable:
      actions.append(ACTION_RESTORE_DISPATCH)` だけで、他 rail の owner approval を
      一切参照しないので、action-scoped authority が valid なときも不適用にならない
      (round 3 review `finding_deliverypartition`)。したがって first-match の
      precedence には入れず、`also_applicable` として並置する。
      `dispatch: uncertain` は block であって retry ではない。
```

### 3.1e selection table

```yaml
selection:
  - id: S1
    when: {issue_state: open}
    primary: recovery
    fallback_if_primary_null: [hibernate, supersede]   # terminal 側から admit する閉集合
    note: "open な issue を勝手に終端化しない。回復が無いときだけ park / 移管を採る。"

  - id: S2
    when: {issue_state: closed, head_integrated: true}
    primary: terminal
    fallback_if_primary_null: []
    note: "recovery rail は precursor (P1) としてのみ plan に載る。R2-R14a は選ばない。"

  - id: S3
    when: {issue_state: closed, head_integrated: false}
    result: escalation
    reason: head_not_integrated_on_closed_issue
    note: "closed だが未統合。終端も回復も自動選択しない (#13841 / #14242 が要求する head 到達性を満たさない)。"

  - id: S4
    when: {}                    # 上記いずれにも該当しない (issue_state: unknown 等)
    result: escalation
    reason: no_phase_applies
```

**指摘された 2 例の機械的な帰結** (round 3 review `finding_phasecomposition`):

- `open` + worker turn failure + `dependency_park` → S1 (primary=recovery)。recovery は R6
  (`refresh-worker`) を返すので **primary は R6**。T2 (`hibernate`) は `fallback_if_primary_null`
  であり recovery が非 null なので**採らない**。plan = `[refresh-worker]`。
- `closed` + `head_integrated: true` + `shell_residue` → P1 が step に入る。S2 (primary=terminal)
  で T1 (`retire`)。plan = `[close-residue, retire]` (この順)。

### 3.2 effect-budget gap (rail は在るが effect budget の選択肢が無い)

`rail: none` の fall-through とは**別種の欠落**がある: subject を持つレールは存在するが、
その effect budget が 1 種類しかなく保存側の選択肢が無い場合。決定木では rule の
`effect_budget` に対する注記として表現し、`rail: none` の fall-through には置かない
(置くと先行 rule が match するため到達不能になる)。

```yaml
effect_budget_gaps:
  - id: EBG-1
    gap: GAP-4
    rule: R9b
    subject: "hibernated + bound + pins absent + server 復元により attestation が stale/absent な pair"
    available_effect_budget: [replace_bad_slots_then_repair_pins]   # 破壊的置換のみ
    missing_effect_budget: session_preserving_metadata_only
    note: >
      R9b (converge-bound-pair) はこの shape の subject を持つので「レールが無い」のではない。
      無いのは「復元された provider session を close せずに pin だけ宣言する」effect budget。
      metadata-only の `repair-pins` (R9a) は `slot_verdict: healthy_no_action` の pair しか
      扱えず、server 復元で attestation を失った pair には適用できない。
      #15811 が ACTIVE 側に作った adopt-restored-pair の hibernated 版に相当する。
```

### 3.3 整合チェック (この doc 内で検算できる形にしてある)

以下は本 doc 自身を `yaml.safe_load` して機械的に検算できる。

1. **軸の閉包**: 全 rule / precursor / selection / co-applicable rule が `when` /
   `intent_by[].when` で参照する top-level key は、`any_axis` を除きすべて `## 2` の
   `pair_shape` に宣言された軸である。
2. **slot fact の閉包**: `all_slots` / `any_slot` / `slot_health.<role>` の中の key は、
   `slot_facts` の 11 軸か、派生値 `slot_verdict` のいずれかである。
3. **値の閉包**: 各条件の値は、対応する軸の宣言値域に含まれる (`slot_verdict` は `## 2.1` の値域)。
4. **到達可能性**: 同一 phase 内で、ある rule の `when` が先行 rule の `when` の**厳密な superset**
   になっていないこと (superset は first-match で到達不能)。`co_applicable_rules` は
   precedence に参加しないので本チェックの対象外。
5. **`yaml` block の parse 可能性**: `yaml` と記した block はすべて `yaml.safe_load` で parse できる
   data である。`evaluate(shape)` と `slot_verdict(slot)` はアルゴリズムの擬似コードなので
   `text` block とし、data と混同できないようにしてある。
6. **gap の到達可能性**: `rail: none` の fall-through (G1 / G2) は、先行する同 phase の rule の
   いずれにも match しない shape に対してのみ発火する。effect-budget gap (EBG-1) は先行 rule が
   match する shape を対象とするため fall-through には置かない。

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
| **OVERLAP-1** | `recover-stale` / `recover-gateway` / `refresh-worker` | **partial** (actuation machinery のみ。shape は排他) | 3 本とも #13806 tranche A/B の同一 actuation (guarded exact-generation close → same-slot launch → action-bound attestation → continuation 1 回)。`worker_turn_recovery` は `gateway_turn_recovery.classify_gateway_turn` を**逐語再利用**し `TURN_CLASS_*` 語彙を共有。ただし決定木では R5 / R6 / R7 が `slot_health.gateway` / `slot_health.worker` / `vanished` で**排他**であり、pair shape が一致することはない | 保護対象の集合が互いに反転している (gateway 保護 / worker 保護 / 両方保護)。staleness 述語が異なる。#14661 j#92369 が「vanished worker と live-but-unproductive worker は別の事実で別の admission」と設計制約として固定 |
| **OVERLAP-2** | `repair-pins` / `converge-bound-pair` | **partial** (durable half のみ。live 側で排他) | 一致するのは **durable row signature** だけ (`hibernated` + `released` + `bound` + pins empty = `not_hibernated_released_bound_pins_empty`)。**live pair 状態では排他**である: `repair-pins` が pin を書けるのは `decide_pair_reconcile` が **GREEN** を返す pair (present / unique / live / idle-or-turn-ended / composer-settled / generation-bound attested) のときだけで、`converge-bound-pair` の subject は逆に「その GREEN を満たさない **stale / unattested** な pair」(`APPROVAL_EFFECT = "replace_bad_pair_then_repair_pins"`)。決定木では R9a / R9b として分離した | actuation 予算が違う (metadata-only vs process 置換)。#13879 は #13847 の precondition を弱めないことを明示目的にしている |
| **OVERLAP-3** | `retire` の 6 intent + 既定 | **partial** (terminal CAS 契約のみ。shape は分割) | 7 経路すべてが「terminal disposition への CAS」を共有するが、`intent_by` 表のとおり `(disposition × worktree_identity × live_pair)` で**互いに排他な分割**になっている。畳めるのは契約であって shape ではない | 各 intent が異なる liveness authority を持つ。#14242 は「ACTIVE row は `process_release == not_requested` なので live-inventory 読取が唯一の liveness authority」であり #13845 より要求が高い、と明記。#14499 は「operator が 5 intent を 1 語彙で読めるよう #14242 を意図的に mirror した」と記録 |
| **OVERLAP-4** | `recover-pair-delivery` / `recover-worker-delivery` / `rehydrate-fleet --execute` の `restore_dispatch` | **conditional true** (下記条件で共適用) | `recover-pair-delivery` (R14a) と `recover-worker-delivery` (R14b) は `delivery_authority.lifecycle_decision_journal` の値で**排他** (`valid_for_this_lane` vs `[absent, mismatched]`)。しかし `rehydrate-fleet` の `restore_dispatch` (R14c) は実装が `if facts.dispatch.sendable: actions.append(ACTION_RESTORE_DISPATCH)` だけを見ており、**他 rail の action-scoped authority が valid でも不適用にならない**。したがって `dispatch: owed` かつ action-scoped authority が valid な shape では **R14c が R14a / R14b と同時に適用可能**になる = その条件下では true overlap。authority が invalid / absent なら R14c だけが残り排他になる。決定木では R14c を `co_applicable_rules` に置き、first-match の precedence に参加させずに `also_applicable` として並置することでこの実態を符号化した | gateway 経由 / worker 直送 / fleet 単位、で経路・要求 authority・effect 契約が異なる。前 2 者は `recovery_effect_contract` の applied-effect / unresolved-fate 契約を共有する |
| **OVERLAP-5** | `reboot-audit` / `rehydrate-fleet` (plan) | **true** (同一 shape) | 決定木 P0 (precursor) が両方を挙げる。同一の per-lane joined facts に対する 2 つ目の per-lane 決定 | `fleet_rehydrate` docstring が明示: #14499 は「この lane はどの disposition へ収束すべきか」、#15745 は「この lane はどの未配送 action を負っているか」で**問いが違う**。#14499 の「lane ごとに違う答え」性質を潰さないため別 planner にした |
| **OVERLAP-6** | `quarantine` / `prepare-bound-pair` / `refresh-worker` | **partial** (`pending_composer` 軸のみ。disposition で排他) | 3 本が「pending composer が前進を塞ぐ」状態を扱うが、決定木では R8 (`active`) / R10 (`hibernated` + bound + pins absent) / R6 (worker が live-unproductive) と前提が分かれる | 承認 gate が別 (`quarantine` の 5 token / `bound_pair_composer_discard_approval` / `worker_refresh_owner_approval`) |
| **OVERLAP-7** | `adopt-restored-pair` / `rebind-restored-pair` | **partial** (`declared_pins` で排他) | 共有するのは「ACTIVE lane の server-restored pair で pin snapshot が現実と合わない」という**問題設定**であり、`declared_pins` の値 (`absent` vs `resolvable`) で排他 (R2 / R3)。両者は既に status 語彙と `slot_reason` を共有している | pin snapshot が absent か stale かで CAS が別物 (empty-only backfill vs exact-2 件 replace)。adopt は rebind より**厳しい** proof chain を要求する (declared pin という照合先が無いため) |

**この分類から出る所見**: 7 件のうち **無条件の true overlap は 1 件 (OVERLAP-5) のみ**、
**条件付き true が 1 件 (OVERLAP-4)**、残り 5 件は「近傍だが排他」な split である。

OVERLAP-4 の判定は 2 度訂正している (round 2 j#109751 `finding_deliveryauthority` で
true → partial、round 3 j#109757 `finding_deliverypartition` で partial → conditional true)。
1 度目は「3 経路の要求 authority が違う」ことだけを見て排他と結論したが、**要求 authority が
違うことと適用可能性が排他であることは別**であり、`restore_dispatch` は他 rail の authority を
参照しないので共適用しうる、というのが実装上の事実だった。したがって「レールが乱立している」という問題の実体は
**同じことを 2 回やっている冗長**ではなく、**近傍の shape ごとに 1 本ずつレールが生えた結果、
集合としての被覆に穴が空いている** (= `## 3.4` の gap) ことにある。統合の主目的を「重複削除」に
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

**前提の訂正 (2 度目。round 3 review `finding_deliverypartition`)**: OVERLAP-4 は
**conditional true** である。`recover-pair-delivery` (R14a) と `recover-worker-delivery` (R14b) は
`lifecycle_decision_journal` の値で排他だが、`rehydrate-fleet` の `restore_dispatch` (R14c) は
実装が `dispatch.sendable` **だけ**を見るため他 rail の authority が valid でも不適用にならず、
**その条件下では 3 経路が同時に適用可能**になる。

したがって本候補は 2 つの別の問いを含む。

1. R14a / R14b は「authority の強さで段階化された 2 経路を 1 rail の権限階梯として表現できるか」。
   畳む場合、`lifecycle_decision_journal` の有無で effect が変わることを型で保てるかが焦点。
2. R14c は 1 と**同じ shape に共適用しうる別 authority の経路**である。畳むなら
   「最も弱い authority (fleet の additive 再送) が、最も強い経路の effect を得ない」ことが
   load-bearing な不変条件になる。ここを緩めると owner 承認なしの再送が承認付き経路の
   effect を得る。

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
9. **統合の主目的の確認**: `## 3.5` の分類では**無条件 true overlap は 1 件 (OVERLAP-5) のみ、
   条件付き true が 1 件 (OVERLAP-4)**、残り 5 件は近傍だが排他な split だった。統合の目的を
   「重複削除」に置くと GAP-2 / GAP-3 / GAP-4 は 1 つも閉じない。Phase 2 の主目的を
   「被覆の穴 (subject gap + effect-budget gap) を閉じる」側に置き直してよいか。
10. **OVERLAP-4 の共適用を仕様として認めるか**: `restore_dispatch` が他 rail の action-scoped
   authority を参照せず共適用しうるのは、実装の意図なのか未整理なのか。owner 承認付き経路と
   承認なし経路が同じ shape に同時適用できる状態を許容するかは authority policy の判断であり、
   本 doc は判定しない。
11. **`## 3.3` の整合チェックを repo に置くか** (本 US では判断しない): 6 項目は現在
    「reviewer / implementer が手元で再実装して回せる形」で記述されているだけで、継続的に
    実行される guard は存在しない。置くなら `tests/` 配下の docs-parity guard が自然だが、
    それは新規 test の追加であり IR (#15841 j#109727) の scope fence「コード・テストの挙動は
    一切変えない (分析 + doc のみ)」の解釈判断を要する。本 doc は現時点で
    **「検算できる形にしてある」ことまでを主張し、「継続的に検算される」とは主張しない**。
12. Phase 2 の受け入れ条件を「本 doc の C1-C5 に列挙した test が 1 本も緑を失わない」で
    固定してよいか。

## 参照

- ADR-0011 (3階層の責務分担) / ADR-0001 (owner 決定の ADR 記録)
- `vibes/docs/logics/managed-state-model.md` (lifecycle 正本)
- `vibes/docs/specs/herdr-native-identity.md` / `vibes/docs/specs/route-identity-ledger.md`
- `src/mozyo_bridge/core/state/lane_pin_role.py` (`PIN_PAIR_*` 語彙の正本)
- `src/mozyo_bridge/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/`
  (全レールの application / domain)
