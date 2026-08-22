# バージョンを所有するプロジェクトコーディネーターの実体化 (Redmine #15844)

- status: active
- version: `v0.1`
- 対象 Redmine: #15844「三階層の運用ハーネス: バージョンを丸ごと持つプロジェクトコーディネーターの実体化」
- 親: #15631「3階層構造を実運用可能にする」/ ADR-0011 (責務分担モデル)
- 読取 base: `origin/main@30aa6aff94de4c00b31c28e605169bcfa9d3e9fc`
- 土台: #15842 (dispatch が silent stall しない) / #15843 (停滞検知) — いずれも main 統合済み

## 本 doc の位置づけ

ADR-0011 は「プロジェクトコーディネーター = Redmine Version (スコープの器) 単位」と定め、
その**最重要責務を drain** (dispatch したサブレーンを終端まで持っていく) としている。本 doc は
その責務を runtime で成立させる方式を固定する。

**ADR-0011 は改訂しない** (owner 裁定 #15631 j#109284: バージョン紐付けは ADR の領域ではなく
運用の話)。本 doc が扱うのは ADR が既に決めた責務を**どの機械が担うか**であり、責務そのもの
ではない。

本 doc は `## 3` の継続追跡だけを実装対象とする。自律 drain / integrate は本 doc が**設計は
するが実装しない** (`## 6` の escalation 対象)。破壊的な自律動作を design_consultation なしに
実装しない、という #15844 j#109958 の authority fence に従う。

**用語 (proposed term)**: 本 doc で「**version tracking snapshot**」とは、1 つの Redmine
Version の issue 集合と、この workspace が所有する lane lifecycle row 集合とを 1 回の read で
join した値を指す。既存の `lane shape` (#15841) / `lane state` (spine `### Lane State
Classes`) とは別語であり、後者 2 つを再定義しない。

## なぜ実体が無かったか — 列挙の起点が lane 側にしかない

「プロジェクトコーディネーター層が実体として存在しない」(#15844 description の owner 指摘) の
機械的な内実は、**既存の列挙面がすべて lane 集合を起点にしている**ことである。

| 面 | 起点 | 落ちるもの |
| --- | --- | --- |
| `sublane reboot-audit` (#14499) | lane lifecycle row 全走査 | 「lane が無い issue」は定義域に無い |
| `workflow drain-queue` (#13967) | **active** lane roster | terminal 化していないが roster から外れた lane、および lane を持たない issue |
| `workflow glance` | lane ごとの durable record | 同上 |
| `workflow dispatch-plan` (#12920) | Version の **open leaf** issue | closed issue を落とすので、**閉じ残りは構造的に見えない** |
| `sublane list` | pane inventory | durable 側にしか無い row |

つまり Version を起点に「配下の全 issue に lane はあるか / その lane は終端に達したか」を問う
面が 1 つも無い。#15789 型の落ち零れ (issue は閉じたのに lane が active のまま残る) は、
**どの面の定義域にも入らないので検出されない**。コーディネーターが 1 件ずつ手渡ししている限り
は手渡しの記憶が代替していたが、それが本 US の言う「実体の不在」である。

`dispatch-plan` が Version 起点でありながらこの穴を塞げないのは重要で、**Version 起点で
あることは十分条件ではない**。`dispatch-plan` は「次に何を dispatch するか」を問うので
`open_leaf_issues` に射影する。閉じ残りは定義上 closed issue 側にあるため、その射影で必ず落ちる。

### 実測 (2026-08-22、本レーンから read-only で取得)

Version #329「v2.2.0 ハーネス/運用整備」を、live Redmine 読取 (`fixed_version_id=329&
status_id=*`) と `state.sqlite` の `lane_lifecycle_records` (`mode=ro`) で join した結果:

| 区分 | 数 | 内訳 |
| --- | --- | --- |
| Version の issue | 60 | — |
| この workspace の lane row | 51 | 33 retired / 2 superseded / 25 active (host 全体) |
| **`drain_owed`** | **2** | #15842 / #15843 — どちらも head は main 統合済み、lane row は `active` のまま |
| `in_flight` | 6 | #15631 / #15642 / #15646 / #15693 / #15748 / #15844 |
| `settled` | 31 | — |
| `undispatched` | 20 | — |
| `umbrella_open` | 1 | — |
| `lane_terminal_issue_open` / `unknown_issue_state` | 0 / 0 | — |
| `unscoped_lanes` (Version 外の非 terminal lane) | 11 | うち issue closed は #15164 の 1 本 (= Version 外の `drain_owed`) |

issue の内訳 2 + 6 + 31 + 20 + 1 + 0 + 0 = 60 で、Version の issue 数と一致する
(数字の出所は `## 3` が実装する `workflow version-track --version-id 329 --json` の
`counts` そのもので、散文用に別途数え直したものではない)。

**2 件は本 doc を書いている時点で実在する閉じ残りである**。#15789 は事後に retired へ落ちて
いるので過去形の実例だが、同じ shape が**今この瞬間に 2 件**ある。これが「手渡しでは回らない」
の測定値であり、本スライスの機構はこの 2 件を検出できることが最低条件になる。

Version 外の 11 本は、version scope を切ること自体が**新しい死角を作る**ことの実測でもある。
`## 3` が `unscoped_lanes` を必ず出力するのはこのためで、version tracking が #15789 と同じ
過ちを別の場所で繰り返さないための構造的な条件である。

## 1. Version binding (PC を Version に束ねる)

ADR ではなく運用の話 (owner 明確化 #15631 j#109284)。置き場の候補は 3 つある。

| 候補 | 置き場 | 得るもの | 失うもの |
| --- | --- | --- | --- |
| B1 | 呼び出し引数 (`--version-id`) | 新しい正本を作らない。schema 変更 0 | 「誰がこの Version を持つか」が durable でない |
| B2 | `.mozyo-bridge/config.yaml` の coordinator-owned 設定 | committed で review 可能。既存 carve-out (`coordinator_operational_config_edit`) が編集権限を既に定義 | Version ごとの binding が commit を要する |
| B3 | `lane_lifecycle_records` に owned_version column | lane と同じ CAS 規律に乗る | **schema bump**。#15706 の実測では additive column 1 本で rewind fixture 系 10 file + offline rollout の target literal 4 箇所が落ちる |

**本スライスは B1 を採る**。理由は effect ではなく順序である: 自律 loop がまだ無いので、
durable binding には**消費者がいない**。消費者のいない binding を先に置くと、B3 なら schema
bump の代償を、B2 なら config surface の拡張を、いずれも「まだ誰も読まない値」のために払う。
一方 B1 で失う durable 性は、当面 Redmine journal 側の dispatch decision (spine
`### Admission Rule`) が既に担っている。

**最終形は escalation 対象** (`## 6` E1)。loop が自律駆動する段では「起動時に自分が持つ
Version を読む」必要が出るので、そのとき B2 / B3 のどちらを採るかは owner の risk tolerance と
config surface の方針に依存する。本 doc はその判断をしない。

## 2. 自律ライフサイクル loop (設計)

バージョンを渡されたら回る機械を 5 段に分ける。**各段は既存 rail を呼ぶだけで、判定の正本を
新設しない**。

| 段 | 何をするか | 正本 / 既存 rail | 本スライス |
| --- | --- | --- | --- |
| L-1 列挙 | Version の issue 集合を読み、lane row と join する | `read_live_fixed_version_bucket` (#13687) + `lane_lifecycle_readonly` | **実装する** |
| L-2 追跡 | 各 issue を `## 3.1` の disposition へ分類する | 本 doc (新規。既存に無い) | **実装する** |
| L-3 終端検出 | `drain_owed` の lane がどの回収レールに乗るか決める | `sublane reboot-audit` (#14499) / taxonomy (#15841) | **しない** — 名指しして委ねる |
| L-4 drain | retire / hibernate / supersede を実行する | `sublane retire` の 7 intent (#13754 系) | **しない** (破壊的) |
| L-5 review 調整 / 返却 | review 承認と integration disposition を集約し version 単位の readiness を返す | spine `## US close と Version close` / `### Completion Semantics` | **しない** |

**L-3 を実装しないのは effect budget の都合ではなく権威の問題である**。どの回収レールに乗るか
の判定は `reboot-audit` が 4 authority join の上で既に持っており (#15841 taxonomy `## 1.A`)、
version tracking がそれを再導出すれば**同じ shape を 2 つの語彙が扱う overlap** を新設する
ことになる。taxonomy が Phase 2 (#15846) で統合しようとしている当のものを増やす向きなので、
version tracking は lane を名指して `sublane reboot-audit --lane-label <lane>` を提示するに
留める。

### 駆動 (どこに置くか)

| 候補 | 評価 |
| --- | --- |
| #15843 の stall watcher 層 | **採らない**。watcher は pane 描画を 50 秒 tick で見る面で、権威は「画面が動いたか」1 つ。version tracking の権威は Redmine + lifecycle store であり、cadence も failure mode も共有しない。同居させると spec `## 既存正本との境界` が禁じる「pane を trigger の正本に昇格させる」向きの圧力がかかる |
| `workflow supervisor` の bounded pass | **推奨**。callback delivery / auto-integration / retire / hibernate の各 leg が既にここにあり (spine `### workspace supervisor の自動退役`)、pass 全体で外部変更上限 1 件を共有する budget も既にある。L-4 を将来足すなら足し先はここ以外にない |
| 独立 background | 採らない。第二 supervisor / 第二 queue の新設は spine が明示的に禁じている |

本スライスは read-only なので**どの駆動にも乗せない** (operator / coordinator が呼ぶ 1 pass)。
supervisor leg への編入は L-4 と同じ round の判断であり、`## 6` E2 で escalate する。

## 3. 継続追跡 (本スライスの実装対象)

`mozyo-bridge workflow version-track --version-id <id>`。**read-only**: Redmine へ書かず、
lifecycle row を書かず、pane を触らず、handoff を送らない。

### 3.0 何を読み、何を読まないか

読む 2 authority:

- **Redmine Version の issue 集合** — `read_live_fixed_version_bucket` (#13687 Increment 1)。
  project 解決 → host 一致 → version 一致 → `status_id=*` の全 issue。leaf / umbrella の判定は
  pure な `mark_leaves` (#12919) がそのまま行う。
- **lane lifecycle row** — `load_lane_lifecycle_readonly` を `repo_workspace_id` で絞ったもの。
  `reboot-audit` と同じ scoping であり、host-global な store から他 project の lane を
  報告しない。

読まない 2 authority: **git** と **live inventory**。この 2 つは「lane をどう回収するか」を
決めるのに要る軸であって (`reboot-audit` がそのために読む)、「何かが owed か」を決めるのには
要らない。読まないことで、herdr が落ちている / worktree が消えている状態でも継続追跡は
答えを返す。**回収不能な lane ほど追跡から消える**、という最悪の失敗の向きを構造的に潰す。

### 3.1 disposition (first-match、全域)

軸は 4 つ。issue の open/closed、この issue を `issue_id` に持つ非 terminal lane row の有無、
terminal lane row の有無、`is_leaf`。terminal は `retired` / `superseded` の 2 disposition
(`managed-state-model.md` の terminal 集合) とする。**`hibernated` は terminal ではない** —
process は解放しているが issue の所有は続いているので drain は依然 owed である。

**issue state の可読性を `is_closed` から読んではいけない**。normalizer
(`_lane_bucket_issue_from_mapping`) は `bool(status.get("is_closed", False))` で読むので、
status object を読めなかった issue は「open」と**区別不能な既定値**で到着する。可読性の証拠は
`status_name` の側にあり (well-formed な Redmine issue は必ず持つ)、これを見ずに既定の False を
open と読むと、読めなかった issue が黙って in-flight 母集団に混ざる。

| # | 条件 | disposition |
| --- | --- | --- |
| 1 | `status_name` が空 (= status を読めていない) | `unknown_issue_state` |
| 2 | 非 terminal lane ≥ 1 ∧ closed | **`drain_owed`** |
| 3 | 非 terminal lane ≥ 1 ∧ open | `in_flight` |
| 4 | 非 terminal lane = 0 ∧ closed | `settled` |
| 5 | 非 terminal lane = 0 ∧ open ∧ terminal lane ≥ 1 | `lane_terminal_issue_open` |
| 6 | 非 terminal lane = 0 ∧ open ∧ lane 0 本 ∧ `is_leaf` が false | `umbrella_open` |
| 7 | 非 terminal lane = 0 ∧ open ∧ lane 0 本 ∧ `is_leaf` | `undispatched` |

**全域性**: rule 1 が可読な行だけを残したあと、rule 2〜3 が「非 terminal ≥ 1」を
closed で二分し、rule 4〜7 が「非 terminal = 0」を closed → `settled`、open → terminal lane
の有無 → lane の有無 → leaf 性、と二分木で尽くす。到達不能な rule は無く、catch-all も要らない
(最後の 2 分岐が leaf の真偽で尽きている)。

**交差 (first-match が隠しうる共適用)**: 1 組ある。

| 交差 | 勝つ側 | precedence_basis |
| --- | --- | --- |
| 2/3 × 6 | 2/3 | `role_precedence` — umbrella であることと lane を持つことは両立する (実測: #15631 は Version 329 の非 leaf でありながら lane `issue_15631_trial` を持つ)。umbrella 判定は **「lane を持たない open issue を undispatched と呼ぶべきか」だけ**に効く軸なので、lane が実在する行では leaf 性を読まない。umbrella に lane がある状態を `umbrella_open` に潰すと、取りまとめ lane の閉じ残りが恒久的に不可視になる |

`unknown_issue_state` を fail-safe として実在させるのは `reboot-audit` と同じ規律である。
読めなかった issue を `settled` と呼ぶことは、**read についての所見を Version についての所見に
見せかける**ことになる。

### 3.2 `unscoped_lanes` (version scope が作る死角の埋め合わせ)

この workspace が所有する非 terminal lane row のうち、`issue_id` が Version の issue 集合に
無いものを、issue 行とは**別の section** として必ず出力する。

分類はしない。`issue_id` / `lane_id` / `lane_disposition` を並べるだけで、それが別 Version の
正当な lane なのか落ち零れなのかは**この面では決めない** (決めるにはその Version を読む必要が
あり、それは別の snapshot である)。名指しだけで十分な理由は、実測 11 本のうち閉じ残りが 1 本
だったこと自体が「version 単位の tracking を複数回す運用」を要求しているからで、本 doc は
その運用を強制しない。

**空にしない**ことが要点である。`unscoped_lanes` が常に出るので、「version-track を回した」が
「host 上の全 lane を見た」を意味しないことが出力から読める。

### 3.3 roll-up と exit code

出力は disposition ごとの **count** と、`drain_owed` / `lane_terminal_issue_open` /
`unknown_issue_state` の行を並べた `attention` list。

**合成 verdict (`integration_ready` のような 1 語) を出さない**。ADR-0011 では version の
統合可否は決定語であってプロジェクトコーディネーターの責務であり、Version close はさらに owner
approval を要する (spine `## US close と Version close`)。read-only の集計面が 1 語で
「ready」と言えば、**それは判断の外見を持つ集計**になる。本スライスが返すのは count であって
button ではない — `reboot-audit` の「roll-up は count であって button ではない」と同じ線を
そのまま引く。

exit code も `reboot-audit` に揃える。

- snapshot が作れた → **0**。owed が何件あっても 0。finding は command の失敗ではなく、消費側の
  loop が `exit != 0` を「見られなかった」と読めなくなるため。
- authority が読めなかった (`RedmineVersionReadUnavailable` / lifecycle store 不読) → **非 0**
  + `state: unavailable`。「見られなかった」を「何も無かった」と報告しない。

closed / locked な Version は `read_live_fixed_version_bucket` の `_require_open` が既に
`version_not_open` で拒否する。**この strictness を version-track のために緩めない**: drain 中
の Version は定義上 open であり、緩めれば「close 済み Version の閉じ残り」という、そもそも
Version close の gate が通してはならなかった状態を追跡することになる。

### 3.4 出力 hygiene

issue の subject / journal 本文 / pane content を一切運ばない。運ぶのは `issue_id` /
`status_name` / `tracker` / `lane_id` / `lane_disposition` / disposition token だけとする
(#15843 spec `## 出力の hygiene` と同じ規律)。これにより 1 pass の JSON envelope はそのまま
durable journal に貼れる。

## 4. コーディネーター interface (設計のみ、本スライス外)

ADR-0011 の「構成済みのバージョンを受け取って回す」を 1 操作にする形。本 doc は形だけ固定し、
実装しない。

```text
coordinator                          project coordinator (L2)
    | 構成済み Version を渡す (1 操作)
    |------------------------------------->|
    |                                      | L-1..L-5 を自律で回す
    |<-------------------------------------|
    | version 単位の integration-ready 返却
```

個々の dispatch / drain を手渡ししない、の**検証可能な形**は次とする (#15844 受け入れ条件 3
「手渡しなしで回る」の判定基準):

- 1 回の Version 受け渡しのあと、coordinator が発行した per-issue の dispatch / drain 操作の
  件数が **0** であること。
- その間に `drain_owed` の最大滞留時間が有限であること (閉じ残りが恒久化しない)。

前者は durable record (Redmine journal) から数えられ、後者は L-2 の snapshot を時系列に並べれば
測れる。**どちらも本スライスの継続追跡が先に在って初めて測れる**ので、実装順序として継続追跡が
先に来る。

## 5. 既存 rail の再利用点 (新規 primitive を作っていないもの)

- **Version → issue 集合**: `read_live_fixed_version_bucket` (#13687)。project 二重解決 /
  host 一致 / version 一致 / `version_not_open` の fail-closed をそのまま継承する。自前で
  `issues.json` を叩かない — それは #13687 が閉じた credential 境界を別の場所で開け直すことに
  なる。
- **leaf / umbrella 判定**: `mark_leaves` / `LaneBucketIssue.is_leaf` (#12919)。`dispatch-plan`
  と同じ判定を使うので、「dispatch 対象と数えるもの」と「undispatched と数えるもの」が一致する。
- **lane row**: `load_lane_lifecycle_readonly` + `repo_workspace_id` scoping。`reboot-audit`
  (#14499) と同一の scoping 規律。
- **terminal 集合**: `managed-state-model.md` の disposition 語彙 (`retired` / `superseded`)。
  本 doc は terminal を再定義しない。
- **回収レールの選択**: `sublane reboot-audit` (#14499) / taxonomy (#15841)。version-track は
  lane を名指すだけで rail を選ばない (`## 2` L-3)。
- **停滞検知**: #15843 watcher。version tracking は「durable に owed か」を見る面で、watcher は
  「pane が進んでいるか」を見る面。**同じ lane について別の質問**であり、どちらも他方を代替
  しない。

### 委譲が実際に繋がることの実証 (2026-08-22 実測)

#15789 は「実行可能な invocation を伴わない代替提示が、存在しない command を coordinator に
推測させた」を欠陥として記録している (#15151 j#108983)。したがって「名指しして委ねる」設計は、
**委ね先が実際に走る**ことを示して初めて成立する。本レーンで read-only に実行した連鎖:

1. `workflow version-track --version-id 329` → `#15843 -> drain_owed`、
   next_step = `sublane reboot-audit --lane-label issue_15843_stall_watcher`
2. その invocation をそのまま実行 → `guarded_close` を選択し、理由を
   「issue #15843 は closed で lane は live managed agent (claude, codex) を保持しているので、
   live pair を閉じる権限を持つ唯一のレールである通常の guarded close で drain せよ」と typed で返す
3. さらに `sublane retire --issue 15843 --lane-label ... --execute` を名指す

**2 の判定には version-track が読まない 2 軸 (git の worktree/統合、live inventory) が効いている**
(`worktree_present=no` / `live=claude,codex`)。境界が正しいことの証拠はここにある: version tracking
が同じ判定を再導出しようとすれば、読まないと決めた authority を読み直すことになる。

## 6. escalation 対象 (design_consultation。本 US では判断しない)

| id | 論点 | 判断が要る理由 |
| --- | --- | --- |
| E1 | Version binding の最終形 (B1 引数 / B2 config / B3 lifecycle column) | 自律 loop の起動時読取が要求する durable 性と、schema bump / config surface の代償のトレードオフ。消費者が現れる段の判断 |
| E2 | 自律 drain / integrate をどこまで許すか | owner の risk tolerance に直接依存する。`retire --execute` は guarded close を伴い、誤判定の代償が「作業を失う」側。#15843 が server-down と frozen を意図的に統合して patience を共有処方にしたのと同じ判断が要る |
| E3 | `lane_terminal_issue_open` を誰が drain するか | spine は「close-ready issue が `着手中` のまま残る状態は durable state の不整合」と言うが、issue の close は coordinator authority であり L2 の自律 close を認めるかは owner 判断。#15841 j#109805 のように US ごとに既定が上書きされる先例もある |
| E4 | `unscoped_lanes` を version-track の責務に含めるか | 本 doc は「名指しだけする」で止めたが、host 全体の孤児 lane を誰が持つかは本 US の scope 外。#15846 (製本 Phase 2) と隣接する |

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/specs/version-owning-project-coordinator.md --repo . --format text`

## Cross-References

- `vibes/docs/adr/adr-0011-three-layer-responsibility-division.md` — 責務モデル (本 doc が
  runtime へ落とす対象。改訂しない)
- `vibes/docs/logics/coordinator-sublane-development-flow.md` — `### Lane State Classes` /
  `### Drain Order` / `### Completion Semantics` / `## US close と Version close` /
  `## サブレーン退役`
- `vibes/docs/specs/recovery-rail-taxonomy.md` — 回収レール 25 本の taxonomy と決定木 (L-3 の正本)
- `vibes/docs/specs/stall-watcher-screen-diff.md` — 直交する停滞センサー (#15843)
- `vibes/docs/logics/managed-state-model.md` — lane disposition / terminal 集合の正本
