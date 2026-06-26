# Acceptance Rerun Protocol (3層 window acceptance smoke)

Redmine #12500 (US) / #12454 / #12473 / #12474 / #12498 / #12499。

親 → delegated coordinator → grandchild gateway-worker の **3層 window/lane 実機
acceptance smoke** が `failed` / `blocked` / `contaminated` / `insufficient` /
`environmental` になった場合に、closed Version を再オープンせず、同等条件で再実行
できる基盤と記録形式を定義する protocol 正本である。

本 doc は protocol 定義であって smoke 実行ではない。実機 smoke の実行は #12498、
結果監査と follow-up close は #12499 が担う。本 doc を読んで #12498 を走らせることは
あっても、本 doc 自体は実機 smoke を起動せず、#12499 を ready に遷移させない。

## What this is NOT

- **closed Version の再オープン機構ではない。** rerun は新規 follow-up ticket
  または同 Feature 配下の追加 US として扱う (`## Rerun follow-up ticket policy`)。
- **smoke 実行手順そのものではない。** 実機実行は #12498。本 doc は再実行の
  baseline / classification / reset / evidence policy を固定する上位 protocol。
- **監査・close 判定ではない。** route/display/metadata/profile 証跡の audit と
  close 判断材料は #12499。
- **Epic / Feature journal に焼く運用方針ではない。** 下記 source-of-truth placement
  を守る。

## Source-of-truth placement (Feature journal を運用方針で汚さない)

`vibes/docs/rules/agent-workflow.md` `Redmine Hierarchy Semantics` のとおり、
Epic / Feature は project の長期機能ポートフォリオ node であり、短期作業の完了単位
ではない。運用方針・失敗分類・rerun checklist の durable 正本は **受入条件を持つ
US (#12500) の description / journals** に置く (#12500 j#64258 correction の根拠)。

```yaml
placement:
  運用方針_失敗分類_rerun_checklist: US (#12500) description / journals + 本 logic doc
  Feature_Epic: portfolio node のみ。operational policy を journal に書かない
  Version: release / milestone / roadmap grouping。rerun のために再オープンしない
```

本 logic doc は US が指す可搬な protocol 本文であり、US journal と二重管理しない。
US journal は run ごとの durable record、本 doc は run 非依存の protocol を持つ。

## Baseline snapshot (smoke 実行前)

rerun を dispatch する **前** に、その run の durable journal へ baseline を記録する。
baseline 不在の run は PASS 証跡として採用しない。pre-existing の blank / 既存 pane を
成功証跡に読み替えない。

```yaml
baseline_snapshot:
  projection:
    - `agents targets` が `KIND` / `DEPTH` / `PARENT` 列を emit するか
    - 対象 pane の現在値 (rerun 前は通常 `- / - / -`)。pre-existing blank は PASS 証跡ではない
  panes:
    - 可視 pane inventory (pane_id / repo / branch / role / workspace / lane)
    - 親 / delegated coordinator / grandchild 候補がどれかを明示
  worktree:
    - runtime-under-test の worktree path / branch / clean 状態
    - 対象 commit が origin (共有 remote) 到達可能か (read-only 確認)
  anchor:
    - sparse target issue とその allowed read boundary
    - durable anchor journal id
  record_timing: dispatch の前に baseline を journal 化する
```

baseline は #12474 j#64133/j#64179 の "current projection baseline" 記録形式を踏襲する。
「列は出るが対象 pane は `- / - / -`、PASS には realization 後の stamp 出現が必要」を
明示し、blank を成功と取り違えない。

## Failure classification

run の結果を次の語彙で記録する。route/display 証跡は product implementation 結果と
**分離して** 評価する (#12474 acceptance focus: product が薄いチケットで blocked でも
route/display は別軸)。

```yaml
classification:
  failed:
    意味: route / realization / display は走ったが acceptance 条件未達
    例: grandchild 必須なのに same-lane worker へ collapse / 必要な stamp が出ず
        `KIND`/`DEPTH`/`PARENT` が blank のまま (#12474 j#64152)
    扱い: 実装 / follow-up へ route する product or route の negative 結果
  blocked:
    意味: 必要な step が前進できず replayable blocked record を残して停止
    例: base prerequisite primitive 不在 (#12473 j#64062) / 対象 commit が origin 未到達 /
        grandchild 必須だが realization 不能
    扱い: PASS/FAIL ではない。blocker を durable に記録し close へ進めない
  contaminated:
    意味: 成功/失敗証跡が stale または cross-context artifact で汚染され無効
    例: receiver が allowed read boundary を越えて management/sibling/prior-smoke
        journal を読んだ (#12474 j#64160/j#64185) / contaminated pane・worktree の再利用
    扱い: PASS/FAIL を出さない。isolation を直して rerun する
  insufficient:
    意味: 許可された証跡 surface が verdict を出すには薄すぎた honest stop
    例: bounded-read 契約で parent lookup が boundary を越え receiver が停止 (#12474 j#64185)
    扱い: PASS でも FAIL でもない。read 方法 / target surface を直して rerun する
  environmental:
    意味: product 以外の tmux / Redmine / landing marker / CLI surface 起因の fault
    例: marker_timeout / pane send 不達 / 必要 CLI surface 欠如
    扱い: product acceptance とは別記録。環境を直して rerun する。runtime を曲げて
          smoke を通さない (`## Prohibitions`)
separation_rule:
  - route 証跡 (parent→child→grandchild delivery が replayable か) と
    display 証跡 (`KIND`/`DEPTH`/`PARENT` が live projection に出るか) と
    product 結果 (対象チケットの実装可否) を別軸で記録する
  - 一つの dimension の PASS を他 dimension の PASS に流用しない
```

## Reset checklist (stale artifact を成功証跡に混ぜない)

rerun 前に次を点検し、stale な pane / worktree / Redmine journal を成功証跡へ
混入させない。window / session / title / display proximity は routing authority では
ない。

```yaml
reset_checklist:
  stale_pane:
    - 直前の contaminated / failed run を処理した pane は退役する
      (#12474 j#64175/j#64178 で `%21`/`%22` を contaminated として除去した例)
    - 親 / lane pane は fresh に作成し、display 近接を route 証拠にしない
    - runtime cleanup のみ。Redmine 証跡や repo branch は削除しない
  stale_worktree:
    - runtime-under-test の worktree が clean で、branch / 対象 commit が想定どおりか確認
    - 対象 commit が origin 到達可能であることを read-only で確認
    - 直前の contaminated run を実行した worktree を無検証で再利用しない
  stale_redmine_journal:
    - 過去 run の journal を現 run の成功証跡として再利用しない。contaminated run の
      journal は history として残し PASS に消費しない (#12474 j#64160)
    - receiver の読取は allowed bounded surface に限定する。management/sibling/prior-smoke
      journal の discovery は contaminated 扱い
pre_rerun_order:
  1. contaminated pane の退役
  2. fresh な親 / lane pane の作成
  3. worktree clean + branch + commit origin 到達性の確認
  4. baseline snapshot の journal 化 (`## Baseline snapshot`)
  5. dispatch
```

## Read boundary / context isolation (insufficient と contaminated の境界)

Redmine が source-of-truth である以上、有能な coordinator は関連 context を自然に
探索するため「完全 context-free」は非現実的である (#12474 j#64172)。bounded-read
契約で線を引き、越えたら `contaminated`、薄すぎたら `insufficient` と honest に分類
する。fabricate しない。

```yaml
read_boundary:
  receiver_may_read:
    - 新規 sparse target issue の body と journals
    - target issue の direct parent の description
  receiver_must_not_read:
    - management / test 管理 issue (例: #12474 のような run 管理 issue)
    - sibling / recent / related issue scan
    - 過去 smoke journal (#12473 / #12460 / prior run) ※ allowed surface 内に明示
      存在する場合を除く
  越境時:
    - allowed surface を越えて読んだら run を `contaminated` と分類 (PASS/FAIL を出さない)
    - allowed surface が verdict に不足なら `insufficient` と分類して停止
  代替:
    - 完全 context-free が無理なら run を明示的に context-rich と宣言し、context-free
      の成功証跡として読み替えない (#12474 j#64160 の revised classification)
```

## Evidence reuse vs reacquire

review-approval、operational-acceptance、owner close approval は別 gate である
(#12473 j#64112/j#64203: "review approval only, not owner close approval";
j#64126: review は実装形状の証明、operational acceptance は downstream smoke で別途確認)。

```yaml
evidence:
  reusable_across_reruns:
    - classical / regression / unit test 結果 (新規 code が land していない限り)
    - resolved docs / catalog consistency
    - 実装形状の code review approval (route/display の operational 証跡とは別)
  must_reacquire_every_operational_rerun:
    - live metadata projection (`KIND`/`DEPTH`/`PARENT` の `agents targets` 上の出現)
    - pane / lane realization 証跡 (grandchild lane の作成 or 明示採用)
    - route delivery marker (parent→child→grandchild の replayable delivery)
    - baseline snapshot
  禁止:
    - prior run の live projection 成功を現 run へ inference で流用する
    - review approval を operational acceptance や owner close approval に読み替える
gate_distinction:
  review_approval: 監査者の実装監査結果。close 承認ではない
  operational_acceptance: 実機 smoke が route/display 証跡を満たした状態
  owner_close_approval: governed close flow の別 journal (本 protocol の対象外)
```

## Prohibitions (rerun 中)

#12473 / #12474 で確定した失敗知見を rerun の禁止事項として固定する。

- **direct cross-lane Claude send をしない。** cross-lane は target lane の Codex
  gateway 経由で same-lane Claude へ route する (#12474 j#64131 constraints)。
- **hidden subagent を使わない。** route 証跡が replayable でなくなる。
- **stale pane / worktree / journal を成功証跡にしない** (`## Reset checklist`)。
- **window / session / title / display proximity を routing authority にしない。**
- **silent same-lane collapse を acceptance にしない。** grandchild が policy 上必須
  なら、realized grandchild lane + stamp を出すか、replayable な `blocked` を記録する。
  決定記録 / same-lane worker handoff で止めて PASS にしない (#12473 j#64151/j#64186)。
- **derivation-only grandchild (panes 空) を PASS にしない。** 宣言された grandchild
  lane は live pane を最低 1 つ持つこと。無ければ fail-closed (#12473 j#64105)。
- **smoke を通すために runtime を編集しない。** CLI surface で不能な step は停止して
  follow-up を起票する。runtime feature 変更で doc step を成立させない。

## Rerun procedure (順序)

```yaml
rerun_procedure:
  1: 過去に contaminated 済みでない新規 sparse target issue を選ぶ / 起票する
  2: target issue の journal に allowed read boundary を明示する
  3: contaminated pane を退役し、fresh な親 / lane pane を作成する
  4: worktree clean + branch + 対象 commit の origin 到達性を確認する
  5: baseline snapshot (projection / panes / worktree / anchor) を dispatch 前に journal 化する
  6: Codex gateway 経由で same-lane Claude / route chain へ dispatch する (direct cross-lane Claude send 禁止)
  7: route → realization → stamp を観測し、各 dimension を分類する
  8: 結果を classification (failed/blocked/contaminated/insufficient/environmental) で
     記録し、route/display を product と分離する
  9: contaminated/insufficient/environmental は isolation/環境を直して rerun。
     failed/blocked は実装 / follow-up へ route する
  10: 残課題は follow-up ticket 化する。closed Version を再オープンしない
```

role-profile chain (`delegated_coordinator` / `implementation_gateway` /
`implementation_worker`) と `KIND=implementation_worker` / `DEPTH=2` /
`PARENT=<delegated>` の projection 期待値は #12498 受入条件に従い、本 protocol の
classification と baseline で評価する。

## Rerun follow-up ticket policy (Version 再オープン禁止)

```yaml
follow_up_policy:
  rerun_の起票単位:
    - 新規 follow-up ticket、または同 Feature 配下の追加 US として扱う
    - closed Version の再オープンを前提にしない
  Version_の役割:
    - release / milestone の完了管理と roadmap grouping (`Redmine Hierarchy Semantics`)
    - rerun のために closed Version を再オープンしない。grouping が必要なら現行
      open Version へ割り当てる
  residual:
    - 残課題は Redmine ticket 化し、暗黙 backlog を残さない (#12499 受入条件)
    - audit / close 判断材料は #12499 に記録する
```

## Acceptance mapping (#12498 / #12499)

- **#12498** が本 protocol に従って実機 smoke を実行する (baseline → reset →
  bounded-read dispatch → classification)。
- **#12499** が route/display/metadata/profile 証跡を監査し、classical
  scenario/regression tests が主要失敗モードを事前検出できることを確認し、残課題を
  follow-up 化し、v0.10.14 acceptance の close 判断材料を記録する。
- 本 doc は両者から参照される protocol 正本であり、自身では smoke を実行せず
  #12499 を ready 化しない。

## Cross-references

- coordinator / sublane / grandchild dispatch 設計: `vibes/docs/logics/coordinator-sublane-development-flow.md`
- `KIND` / `DEPTH` / `PARENT` projection と Unit/Target model: `vibes/docs/logics/unit-target-model.md`
- destructive acceptance / baseline / operator gate の先例: `vibes/docs/logics/turnkey-e2e-acceptance.md`
- cross-project handoff smoke と Codex gateway route (direct Claude send 禁止): `vibes/docs/logics/cross-project-cockpit-smoke-runbook.md`
- tmux send safety (landing marker / environmental fault): `vibes/docs/logics/tmux-send-safety-contract.md`
- Redmine 階層と Feature/US/Version の責務: `vibes/docs/rules/agent-workflow.md` `Redmine Hierarchy Semantics`
- classical test frame companion work: Redmine #12474 j#64209 / j#64217
- durable anchors: Redmine #12454 / #12460 / #12473 / #12474 / #12498 / #12499 / #12500
