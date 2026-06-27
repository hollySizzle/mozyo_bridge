# delegated coordinator real-machine acceptance

Redmine #12498 / #12499。GK3500 / IT Operations のような外部親 project から、
mozyo_bridge delegated coordinator を能動的に起動し、さらに孫 implementation lane へ
委譲する 3 層 real-machine acceptance の正本。

本書は実機 acceptance の **何を合格とみなすか** を固定する。classical tests への分解は
`delegated-coordinator-smoke-test-frame.md`、cross-project cockpit の汎用操作は
`cross-project-cockpit-smoke-runbook.md`、既存 project adoption の運用境界は
`existing-project-sublane-adoption.md` を参照する。本書へ private project 固有の pane id、
absolute path、cockpit 並び順を焼き込まない。実行時の具体値は Redmine journal に残す。

## 目的

実機 acceptance が確認するのは、単に mozyo_bridge 内で 3 層 projection を作れることでは
ない。外部親 project が抽象的な依頼から自律的に子 project を選び、子 project が必要なら
孫 implementation lane を開き、全階層が visible window/lane と durable journal で replay
できることである。

合格した状態:

- 親 project coordinator が、過剰な test hint なしに依頼を読み、自 project config
  (`project.yaml` 相当) や durable work context から mozyo_bridge を delegated child として
  選ぶ。
- mozyo_bridge delegated coordinator が子 window/lane として起動または明示採用される。
- mozyo_bridge が必要な child ticket / implementation request を durable work system に
  作り、親 callback route と close authority を親側へ残す。
- 実装作業を delegated coordinator 自身の context に載せると後続 callback / audit を圧迫
  する場合、孫 implementation gateway / worker lane を起動または明示採用する。
- 親 / 子 / 孫が cockpit 上で別 window/lane として区別でき、Redmine journal から route /
  creation-or-adoption / profile / callback / result を replay できる。

## Test Models

### Context-rich smoke

既存 management issue、親子孫候補、pane id、期待する route を operator が明示してから
走らせる model。route 部品の debug には有用だが、外部親 project が自律判断した証明には
ならない。context-rich run を full acceptance PASS にしてはいけない。

### Bounded-read smoke

receiver が読む durable surface を限定して context contamination を検査する model。
親が読める issue body / journals / config surface を明示し、禁止 surface を読んだ場合は
`contaminated` として PASS/FAIL に混ぜない。禁止 issue id の列挙自体が hint になる場合は、
読める surface 自体を fixture / sparse issue で小さくする。

### Autonomous parent smoke

本命の acceptance model。親 project には抽象依頼と通常運用で読むべき最小 durable anchor
だけを渡す。親子孫候補、pane id、expected route、mozyo_bridge を起動すべきという答えを
渡さない。親が project config と依頼内容から delegation を判断できるかを見る。

## Current Roadmap Gates

Redmine #12499 の現在の合格目標は #12546 autonomous parent real-machine smoke である。
ただし、#12546 を実行する前に、repo-local の準備 gate を次の順序で満たす。

1. #12590: `src/` と `tests/` の bounded-context layout full expansion を完了し、以後の
   smoke / scenario が古い巨大 file 配置へ引き戻されないことを確認する。
2. #12608: queue-enter default rail を、sublane Codex から same-lane Claude への実機
   dogfood delivery として確認する。anchor が入らない、Enter が効かない、retry window 内に
   submit-complete しない場合は #12546 の環境要因に混ぜず、#12608 側で止める。
3. 上記後に scenario / oracle を再実行し、classical test が実機 smoke より粗い振る舞い検査へ
   退化していないかを audit する。

#12603 / `sublane lifecycle and worktree integration late window` planning bucket の sublane Git/non-Git lifecycle hardening は、#12546 後の統合強化であり、
新しい finding が依存関係として再分類されない限り #12546 の前提 gate ではない。

この順序は mozyo_bridge repo-local の運用順序であり、3 層 acceptance の普遍条件ではない。

## Parent Prompt Boundary

親 project coordinator へ渡してよいもの:

- 通常運用で owner が出す程度の抽象依頼。
- durable work system の起点 issue / journal pointer。
- 親 project が通常参照する local project docs / config。
- operator が実運用でも指定する制約 (credential 禁止、destructive 禁止、Redmine replay
  必須など)。

渡してはいけないもの:

- mozyo_bridge を子として起動せよ、という期待答え。
- 子 / 孫の具体 pane id、worktree path、lane id。
- 既存 smoke management issue の PASS/FAIL journal。
- `delegated_coordinator` / `implementation_gateway` /
  `implementation_worker` を使えば合格、という test oracle。
- 親が `project.yaml` を読んで判断すべき内容の先回り要約。

これらを渡した run は、たとえ 3 層 window が開いても context-rich / harness-assisted run で
あり、本命 autonomous parent smoke の PASS ではない。

## Expected Route

1. 親 project coordinator が抽象依頼を受ける。
2. 親 project coordinator が project config / durable work context を読み、子 project
   candidate を決める。
3. 親 project coordinator が parent delegation decision を Redmine に記録する。
4. 親 project coordinator が子 project の Codex gateway / delegated coordinator へ handoff
   する。cross-project Claude direct send は禁止。
5. 子 delegated coordinator が必要な child issue / implementation request を durable work
   system に起票または特定する。
6. 子 delegated coordinator が孫 dispatch の要否を判断する。不要なら
   `grandchild_dispatch: avoided` と理由を記録する。必要なら visible grandchild lane を
   起動または明示採用する。
7. 子 delegated coordinator が孫 lane を stamp し、`KIND` / `DEPTH` / `PARENT` の live
   projection を確認する。
8. 子 delegated coordinator が孫 Codex gateway へ handoff し、孫 gateway が same-lane
   Claude worker へ渡す。
9. 孫 worker は fresh projection を自分で観測し、route/display/profile evidence と product
   result を分離して記録する。
10. 孫 -> 子 -> 親へ callback outcome を返す。

## Acceptance Criteria

Full PASS に必要な条件:

- 親 project は operator から expected route を教えられずに child delegation decision を
  出している。
- child delegation decision は parent config / project map / durable issue context のどれを
  根拠にしたかを journal に残している。
- 子 mozyo_bridge delegated coordinator は別 window/lane として visible である。
- 孫 implementation gateway / worker は、必要な work shape の場合に別 window/lane として
  visible である。
- live projection は少なくとも `DEPTH=2` と `PARENT=<delegated coordinator>` を示す。
  `KIND` の語彙が acceptance text と runtime projection で違う場合は silent PASS にせず、
  audit item または follow-up として記録する。
- role profile chain は `delegated_coordinator` / `implementation_gateway` /
  `implementation_worker` として delivery record に残る。
- Redmine journal から baseline、parent decision、child handoff、grandchild realization、
  worker confirmation、callback outcome を replay できる。
- ticketless consultation が `implementation_not_needed`、`no_dispatch`、`blocked`、
  `anchor_required` で止まる場合でも、receiver が caller lane へ structured
  `consultation_result` / hands-off を返している。pane 上の自然文回答だけを callback
  evidence にしていない。
- stale pane / stale worktree / stale journal を success evidence に使っていない。
- direct cross-project Claude send と hidden subagent を使っていない。

## Failure Classification

- `failed_acceptance`: 親が自律 delegation できない、子 / 孫 window が必要なのに起動しない、
  direct Claude send など invariant 違反がある。
- `insufficient`: 3 層 projection の一部だけ見えたが、外部親 project 起点の autonomous route
  ではない、または parent prompt に test oracle が混入している。
- `contaminated`: receiver が禁止された management issue / prior smoke / injected route
  context を読んだ。
- `blocked`: required tool / pane / config / durable work system が不足し、PASS/FAIL を判断
  できない。
- `environmental`: marker timeout、tmux focus、network、Redmine write など環境要因の非合格
  試行。successful route evidence に混ぜない。

## Classical Test Obligations

real-machine acceptance の前に、classical tests は少なくとも次を検出できる必要がある。

- parent prompt に explicit child / grandchild route が混入した run を autonomous PASS に
  しない。
- project config から child project candidate を解決できない場合に fail-closed する。
- parent -> child handoff は Codex gateway 宛てで、cross-project Claude direct send を
  生成しない。
- grandchild required なのに same-lane worker へ fallback した場合、
  `grandchild_required_but_not_realized` として止める。
- synthetic projection / fixture で `KIND` / `DEPTH` / `PARENT` の chain を検証する。
- Redmine read surface に parent journals / sibling / prior smoke が混入した場合、
  `contaminated` として PASS/FAIL を出さない。
- command planner は parent decision、child handoff、grandchild stamp、worker handoff、
  callback record の順序を生成する。

実機 smoke はこれらを初めて発見する場ではない。実機で見るのは、実 tmux / Redmine /
cockpit / project config が classical contract 通りに接続されるかである。

## Redmine Record Package

最低限残す journal:

- Start Gate: test model (`autonomous_parent`, `bounded_read`, `context_rich`) と非目標。
- Baseline: fresh panes / worktrees / target project config snapshot。
- Parent Decision: child project を選んだ根拠、読んだ config、渡されていない hint。
- Ticketless Callback: no-dispatch / blocked / anchor-required で止まった場合の caller lane
  への hands-off result。Redmine anchor が無い段階では、callback transport と
  `next_action_owner` を明示する。
- Child Delivery: role profile `delegated_coordinator` と target repo gate。
- Child Result: issue 起票 / implementation request / grandchild dispatch decision。
- Grandchild Realization: visible lane/window、stamp、projection。
- Worker Evidence: worker 自身の fresh projection と product result。
- Callback Outcome: required callback target 全件。
- Final Classification: PASS / failed_acceptance / insufficient / contaminated / blocked /
  environmental と理由。

## Relation To Existing Docs

- `delegated-coordinator-smoke-test-frame.md`: 本書の acceptance を classical tests と薄い実機
  smoke に分解する test frame。acceptance の正本ではない。
- `existing-project-sublane-adoption.md`: 既存 project adoption と external-submodule 委譲の
  runbook。context-free smoke の境界は本書を参照する。
- `cross-project-cockpit-smoke-runbook.md`: cockpit append/adopt/discovery/handoff の汎用
  操作手順。親が自律 delegation したかの acceptance は本書で判断する。
- `delegated-coordinator-cockpit-display.md`: window/lane display metadata と projection の
  設計正本。
- `delegation-policy-project-config.md`: delegation policy knob と fixed invariant の正本。
- `delegated-coordinator-role-profile.md`: role profile 語彙と handoff contract の正本。
