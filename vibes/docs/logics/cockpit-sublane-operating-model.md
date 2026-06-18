# Cockpit サブレーン運用モデル

## Purpose

この文書は Redmine #11850 の multi-lane cockpit PoC から出てきた運用哲学を
記録する。これは repo-local な logic 文書であり、private な社内運用規約
そのものではない。

目的は、mozyo-bridge の portable primitive を濁さずに、実運用で発生した
圧力と判断を後から検証できる形で残すことである。

## 観測された前提

cockpit は、同じ workspace の複数 checkout を別 lane として扱えるように
なった。現在の PoC では、次の形で運用している。

- main `mozyo_bridge` lane は coordinator lane である。
- 追加 worktree は sublane として append する。
- 各 lane には Codex pane と Claude pane がある。
- Redmine journal が durable source of truth である。
- pane message は durable anchor への pointer にすぎない。

このモデルが必要になったのは、実際に dogfooding して初めて見える問題が
複数あったためである。

- same workspace / multiple checkout identity には `lane_id` が必要である。
- cockpit と通常の `mozyo` session は、window name だけではなく role
  resolver で扱う必要がある。
- sublane 側で作業が完了しても、結果が coordinator lane に戻らなければ
  cockpit 上では停止して見える。
- identity が正しくても append 後の表示幅が偏ることがある。
- 開発中は installed CLI が repo-local source より遅れることがある。
- 関連する複数 project は近くに見える必要があるが、routing identity まで
  混ぜてはいけない。

## 中核の分離

cockpit model では、次の 4 つを混同してはいけない。

- **Identity**: workspace / lane / role / pane の durable な事実。
- **Routing**: どの agent が handoff を受け取り、行動してよいか。
- **Display**: pane、window、tab、iTerm/tmux view の見せ方。
- **Governance**: どの Redmine gate が実行や close を承認しているか。

window layout は人間が関連作業を見やすくするためには有用だが、routing の
source of truth ではない。隣に pane が見えていることは、lane 境界や
project 境界を越えた direct send の承認にはならない。

## Lane の役割

### Main Coordinator Lane

main Codex pane は coordinator、auditor、owner-facing window である。主に
次を扱う。

- owner への質問と close approval の回収。
- Redmine gate の解釈。
- review conclusion。
- release / push / CI の coordination。
- sublane の作成と退役。
- sublane bandwidth / admission / drain 判断。
- PoC findings の Redmine または repo-local docs への記録。

main Codex lane は direct edit に慎重であるべきである。project rule が許す
repo-local guardrail autonomous lane は使ってよいが、通常の実装や配布対象の
workflow surface は project の role boundary に従う。

coordinator は単に止める actor ではなく、throughput owner でもある。実装可能な
ready work があり、[[logic-sublane-bandwidth-policy]] の admission check を
満たす場合は、専用 sublane / worktree を作り target-lane Codex gateway へ
dispatch するのが通常動作である。main lane に実装を抱え込むこと、または
main-unit Claude を実装 lane の代替にすることは、parallel cockpit model の効率を
落とすため避ける。逆に、blocking design decision、重複 file / invariant、owner
decision、release / credential / destructive gate、callback backlog がある場合は、
serial 化または drain を選び、その理由を durable journal に残す。

### Sublane Codex

sublane Codex pane はその lane の gateway である。主に次を行う。

- まず durable Redmine anchor を読む。
- request が自 lane に属することを確認する。
- local Claude に実装させるべきか判断する。
- durable journal anchor 付きで local Claude へ route する。
- blocked / review-ready / owner-action-needed の状態を coordinator lane へ
  返す。

sublane Codex は、project が明示的に昇格させない限り、第二の owner-facing
coordinator ではない。

### Sublane Claude

sublane Claude pane は implementation worker である。主に次を行う。

- pane scrollback だけではなく Redmine journal から実装する。
- implementation_done と review_request gate を記録する。
- verification と residual risk を再現可能に残す。
- owner close approval を回収しない。

### Main Claude

main Claude pane は有用だが、parallel coordinator にしてはいけない
(Redmine #11858)。これは特定 model の能力評価ではなく、観測された workflow
risk に基づく境界である。main unit は owner-facing / audit / routing の source
of truth に最も近い pane であり、ここで gate / owner 判断を silent に行う actor
が混ざると、multi-lane model 全体が依存している分離が崩れる。

main lane の Claude output は input であって evidence ではない。coordinator
Codex は、それを decision に変換する前に source file、Redmine journal、
command output で確認しなければならない。pane scrollback と同じく「確認すべき
pointer」であり durable な事実ではない。

安全な使い方 (coordinator Codex の context を節約できる concrete task) は次で
ある。いずれも authoritative な決定を生まないものに限る。

- 長い Redmine journal / diff / log / command 出力の要約 (coordinator が後で
  検証する)。
- candidate 抽出 (stall candidate の一次列挙、changed paths、影響 issue など)
  を、coordinator が durable record と突き合わせる前段として作る。
- scratch analysis と read-only 調査 (durable edit を残さない)。
- draft wording (journal 文面、next-action menu 案、doc 段落)。coordinator が
  review して own してから land させる。
- option の非権威的な比較。
- work が適切な Redmine-gated lane (専用 sublane / worktree) に移された後の
  implementation。これは「main unit assistant」用途ではなく、bounded lane で
  の通常の implementer 用途であり、標準の implement → record → review flow に
  従う。

main Claude に任せるべきではないもの (coordinator Codex が保持する) は次で
ある。request が Claude pane に直接打ち込まれても変わらない。

- owner questions、owner close approval の solicit / collect / ratify
  (owner 承認は単一 coordinator Codex に集約する。`owner 承認待ちの集約`
  参照)。
- Review Gate / US audit conclusion、review verdict の記録。
- durable routing decision (どの lane に渡すか、lane 境界を越える handoff の
  dispatch、sublane done の判断)。
- Redmine gate を満たしたと解釈する、gate を進める、issue を close する。
- protected workflow / skill / source / test surface への silent edit、または
  gated lane 外の編集。

main Claude が implementation request を受け取った場合は、次の境界で止める。

- まず durable Redmine anchor を読み、実装前に明らかな設計矛盾・scope
  不足・invariant 衝突があれば Design Consultation を起票してよい。
- Design Consultation、read-only 調査、reroute 用の事実整理を終えたら、
  main Claude はそこで停止する。実装 diff は出さない。
- coordinator Codex は、その issue に専用 sublane / worktree を作り、target-lane
  Codex gateway 経由で same-lane Claude へ実装を渡す。
- main Claude は、専用 sublane へ明示的に移されない限り、自分の main
  worktree で source / tests / scaffold / guardrail 実装を始めない。

Redmine #11955 はこの境界の具体例である。scaffold option 実装依頼が一度 main
Claude に届いたが、main Claude は実装前に `catalog refs install` と
`catalog.yaml` 非変更 invariant の衝突を Design Consultation として記録した。
coordinator Codex はその回答を Redmine に残し、main Claude を停止させ、専用
`issue_11955` sublane / worktree へ実装を reroute した。この flow が正であり、
main Claude がそのまま実装する flow は再発させない。

sublane Claude との違い: sublane Claude は自 lane の gate 下で実 diff を出し、
implementation_done / review_request を記録する bounded implementation worker
である (上の `### Sublane Claude`)。main Claude は自前の implementation lane を
持たず、work が gated lane へ明示的に移るまで assistant-only (要約 / 抽出 /
draft / scratch) にとどまる。in place で実装させると、unreviewed edit が audit /
owner-facing source of truth の隣に並び、本節が守ろうとする risk そのものに
なる。

## Cross-Lane Routing Rule

複数 pane が同じ物理 tmux session にいても、lane boundary は governance
boundary である。

request が lane を越える場合は、まず target lane の Codex pane に route
する。その Codex が durable anchor を読み、implementation が適切であれば
local Claude へ route する。

Claude への direct delivery は same-lane addressing に限定する。これは
cross-session Claude direct-send prohibition と同じ原則を守るためである。

## Same-Lane Claude Dispatch の Submit 完結

target lane の Codex が同一 lane の Claude implementer へ実装 / review request を
渡す same-lane `--to claude` hop (`## Cross-Lane Routing Rule` が sanctioned route と
する local hop) は **標準 handoff であり、submit まで完結する**。typed-but-unsubmitted
な pending 入力で止めない。pending のまま放置すると operator / coordinator が手で
Enter を押す必要が生じ、これは本 model が排除しようとする stall である (Redmine
#12207 j#60739 / j#60741: same-lane Codex→Claude dispatch が `blocked` を返しつつ通知が
Claude pane に staged のまま残り、coordinator が手動 Enter で unblock した事例)。

- default の `queue-enter` rail は、Claude pane が自 window の active split のとき
  `--mode` 不要で submit (Enter) する。
- Claude pane が inactive split (Codex pane が active な cockpit grid の通常形) のとき
  `queue-enter` は active-split gate で fail-closed (`blocked` / `invalid_args`) し何も
  type しない。block が出力する recovery command どおり `--mode standard --target
  <claude_%pane> --target-repo auto` で再 dispatch する。`standard` は landing marker を
  観測してから Enter するため、active-split guard を緩めずに inactive な same-identity
  pane へ submit 完結する。これは coordinator callback が (やはり通常 inactive な)
  coordinator pane に対して `--mode standard` を使うのと同じ選択である
  (`skills/mozyo-bridge-agent/references/workflow.md` `### Callback procedure`)。
- `--no-submit` / `--mode pending` は標準 dispatch 経路ではない。通知を type して
  operator submit のため pending に残す明示的な operator / debug fallback (および
  per-preset `marker_timeout` retry path) であり、same-lane Claude への routing default
  にはしない。標準 dispatch でこれを選ぶことが #12207 が直す regression である:
  dispatch は delivered に見えるのに implementer は turn を受け取らない。
- dispatch outcome (sent / blocked-with-reason + replayable retry command) を callback と
  同様に durable record に残す。recovery command を伴う `blocked` は dispatch を
  replayable に保つ。pending に黙って残した dispatch は delivered handoff ではない。
- submit rail そのものの定義は `vibes/docs/logics/tmux-send-safety-contract.md` が正本
  (`queue-enter` default / `standard` strict explicit fallback / `pending` operator-submit
  path)。本節は same-lane dispatch が pending に停まらず submit へ進むよう *どの rail を
  使うか* を固定するだけで、active-split / receiver-binding / process gate を緩めず、
  blind Enter も導入しない。

## Cockpit Groups

関連 project は同時に見える必要がある。portable rule は次である。

- named cockpit session を cockpit group として使う。
- group 内でも `workspace_id` / `lane_id` / role / pane identity は維持する。
- iTerm window、tab、tmux window は display grouping としてのみ扱う。
- cross-project consultation には Codex gateway handoff を使う。

無関係な project policy を OSS default に入れてはいけない。private cockpit
composition は private operating policy の領域であり、portable mozyo-bridge
default に混ぜない。

## Dogfooding Version Boundary

開発中は installed `mozyo-bridge` CLI が repo-local source より遅れることが
ある。workflow が landed 直後の command に依存する場合は、repo-local
invocation を使う。

```bash
PYTHONPATH=src python3 -m mozyo_bridge ...
```

これは dogfooding rule であり、public install contract ではない。public docs
では release 後の installed command を説明する。

## Coordinator への報告

sublane は handoff-worthy な state transition を Redmine と短い pane pointer
で coordinator lane へ返す。例は次である。

- blocked / needs clarification。
- implementation_done。
- review_request。
- review result。
- commit recorded。
- owner close approval requested。

これにより、sublane では完了しているのに cockpit coordinator view では停止
して見える状態を避ける。

## Coordinator の停止点と次アクション提示

coordinator lane が慎重であること自体は正しい。close を owner approval で
gate し、guardrail を bypass しないのは設計どおりである。問題は、停止時に
次アクションを提示しないと、その coordinator に依存する sublane も idle し、
cockpit 全体が詰まって見えることである (Redmine #11860, #11850 PoC 由来)。

解決は「coordinator を雑にする」ことではなく、「停止を構造化する」ことで
ある。観測された portable な判断は次である。

- **停止は失敗ではなく正常状態。** owner 判断待ち / close 待ち / review 結論
  待ち / 次作業選定待ちは想定内の停止である。標準化するのは停止の有無では
  なく、停止の記録と提示の仕方である。
- **durable record が先、pane pointer が後。** 停止理由と次アクション候補は
  Redmine journal に残し、pane 通知はその pointer にとどめる。停止理由を
  scrollback だけに置かない。
- **自律可能範囲と owner 承認範囲を分ける。** durable record から取れる
  action (要約、review finding 記録、承認済み実装の sublane への routing、
  backlog task の空き sublane への dispatch、autonomous lane 編集) は停止せず
  実行する。停止するのは残る next action が owner 承認範囲 (`Close Approval
  Separation` と carve-out) だけになったときに限る。
- **停止時は三点を短く提示する。** (1) なぜ止まるか (待っている gate を
  journal id 付きで)、(2) owner が承認したら何をするか (承認直後の具体
  step)、(3) 承認なしに進められる代替作業があるか (空き sublane / backlog)。
  無ければ「無い」と明示する。
- **gate された作業は queue に戻す。** 一単位が owner 判断待ちでも、ready な
  非 gate task は next-action queue / backlog から空き sublane へ dispatch し、
  cockpit 全体を gated item で止めない。coordinator 待ちの sublane には、
  依存先 journal anchor を sublane issue に記録して park させる。
- **新規 dispatch は bandwidth admission を通す。** 空き pane / worktree が
  あることは dispatch 条件ではない。unread review_request、owner_waiting、
  blocked callback、retire_ready lane が coordinator attention を消費している
  場合は、[[logic-sublane-bandwidth-policy]] の admission / drain order に従う。

提示は提案であって自己承認ではない。「承認されたら close する」と書いても、
別 journal の owner close approval なしに close してよいことにはならない。
owner-facing なやり取りは coordinator lane に残し、sublane の Claude pane で
owner 承認を回収しない。

具体的な next-action menu、throughput 目標、どの backlog を先に消化するか
等は operator runtime policy であり OSS default に混ぜない (public-private
boundary)。portable な部分は「停止ごとに durable な理由 + 三点提示を残し、
ready な作業を queue に戻す」ことである。

mozyo_bridge dogfooding の soft profile と dispatch burst の記録方法は
[[logic-sublane-bandwidth-policy]] に置く。core CLI や shared skill の default
として並列上限を固定しない。

## owner 承認待ちの集約

停止点標準 (#11860) と sublane callback (#11852) の交差で残っていた前提を
#11867 で明示した。owner 承認待ちは、それが発生した sublane 内で完結させて
はならず、必ず単一の owner-facing 点 = main coordinator Codex に集約する。
#11855 / #11860 では review approved / owner-close-waiting が Redmine には
記録されていたが、coordinator に集約されないと cockpit 上では停止して
見えた。pane が増えても「いま owner を待っている issue はどれか」を pane
ごとに探し回らせない、というのが要点である。

観測された portable な判断は次である。

- **owner 承認を回収する actor は一つ。** main coordinator lane の Codex
  だけが owner 承認を回収する。sublane の Codex / Claude は待機状態を
  durable record に記録して callback するだけで、自 pane で owner 判断を
  solicit / collect / ratify しない。これは `Result Notification Boundary`
  と central preset の `Owner Close Approval Delegation` を集約方向に適用
  したものである。
- **owner-approval-waiting の二状態を明示する。** owner close approval
  waiting (Review Gate / US audit 後、`Close Approval Separation` 待ち) と、
  owner-action-needed (scope / stakeholder 判断、carve-out、owner-only
  unblock、owner が答える Design Consultation など、close approval より
  広い owner 判断)。後者を「blocked」に潰さず別状態として扱う。
- **承認待ち集合は pane 非依存で列挙する。** owner-approval-waiting 集合は
  durable record の属性であり、pane scrollback / `status` / `doctor` を
  pane ごとに走査して作らない。Redmine 上の gate journal / status を query
  して再構成する。待機は pane ではなく issue に乗っているので、pane が
  非表示でも sublane が退役しても集合から落ちない。callback は集合そのもの
  ではなく pointer であり、coordinator は受け取った callback から queue を
  組み立てるのではなく durable record から導出する。
- **集約は自己承認ではない。** coordinator に集約しても、別 journal の owner
  決定なしに owner の代わりに承認してよいことにはならない。standing
  delegation 下でも carve-out を self-authorize しない。

owner の承認待ち列挙に使う具体的な Redmine filter / saved query / status
mapping と、その列の優先順位付けは operator runtime policy であり OSS
default に混ぜない。portable な部分は「owner 承認待ちは単一 coordinator
Codex に集約し、pane 数に依存せず durable record から列挙できる」ことで
ある。

## stall / no-progress 検出 (#11880)

sublane callback (#11852) は happy path、つまり sublane が進んだ durable state
を pointer で返す経路を定義する。だが callback は best-effort な pointer で
あり、cockpit が育つと単に届かないことがある。sublane Codex が routing
callback を記録しなかった、durable record は進んだが誰も pointer を残さな
かった、send 自体が target 解決に失敗した、などである。届かないと coordinator
は沈黙だけを見て、その lane が blocked か still-working か done かを手で
Redmine / worktree / pane を polling して判定することになる (Redmine #11880,
#11854 PoC 由来)。#11880 はこの検出を durable-record anchored に機械化する。

観測された portable な判断は次である。

- **stall candidate は durable record から定義する。** stall candidate は
  「handoff が delivered で、期待される次の durable journal が tolerance
  window 内に現れない」work unit である。delivered は dispatch journal
  (Start / implementation_request / coordinator routing journal) が issue 上に
  存在すること。未到来は、その dispatch が待っていた gate / Progress Log
  journal の不在。どちらも Redmine issue から読み、pane scrollback から
  読まない。pane 沈黙 / 空の `status` `doctor` / clean worktree は corroborating
  signal どまりで trigger ではない。lane は何も返さず作業中のこともあり、
  done で callback だけ欠けることもあるので、沈黙では区別できない。trigger は
  「delivered dispatch journal + 期待 durable journal の欠如」であり、issue
  だけから再構成でき、pane 退役後も残る。
- **どの次状態を待っているか分類する。** 単一の「stalled」に潰さず、durable
  record から四状態に分類する (#11880 j#57539)。
  - `no_progress_after_handoff`: delivery 成功だが新しい durable journal が皆無。
  - `progress_without_callback`: 新しい durable journal はあるが coordinator
    callback / ack が無い。作業は止まっておらず pointer だけ欠落。
  - `callback_delivery_failed`: callback を試みたが send が失敗 (target 解決 /
    window-binding preflight / stale-CLI rejection)。試行の durable record を読む。
  - `callback_not_attempted`: durable progress はあるが callback も receive-method
    journal も無い。sublane 側の process gap。
  分類は issue の最後の journal を読み「blocked / no-progress / still-working /
  implementation_done のどれを待っていたか」で決め、pane では決めない。
- **stall check と再通知は durable journal に残す。** stall candidate と判断し
  再通知 / escalation したら、その事実 (分類・欠落内容・再通知先) を issue に
  Progress Log として記録する (#11854 j#57526 と同型)。journal を残さない
  silent re-poke は次 coordinator から不可視であり禁止。`progress_without_callback`
  の解決は「既に進んでいた state を直接拾った」と記録し、done な work を
  re-dispatch しない。
- **stale CLI は handoff/callback 中の独立した stall mode。** lane が idle では
  なく tooling が壊れていて callback が欠けることがある。target-lane Codex は
  生きて reasoning しているが、stale installed CLI (例: `agents targets` を
  知らない古い `mozyo-bridge`) に dispatch / callback が blocked され、routing
  callback journal が記録されなかった (#11880 j#57555)。これを
  `callback_delivery_failed` の sub-case として扱い「no progress」と誤読しない。
  active dogfooding handoff path では installed CLI が source に lag しうるため、
  release / install が追いつくまで repo-local CLI (`PYTHONPATH=src python3 -m
  mozyo_bridge ...`) を優先する。stale-CLI stall を解いた coordinator
  intervention は issue に durable Progress Log として残し、target-lane Codex
  gateway model の置換ではなく一時的な dogfooding intervention と理解する。

これは停止点標準 (#11860) や owner 承認集約 (#11867) を緩めない。検出された
stall も同じ durable journal、同じ next-action 提示、同じ単一 owner-facing
集約点で解決する。stall 検出は state を見つけるだけで、close / carve-out /
owner 判断を self-authorize しない。

具体的な tolerance window (どれだけで「遅い」か)、stall candidate を列挙する
Redmine saved query / filter、private な再通知 cadence / escalation 順は
operator runtime policy であり OSS default に混ぜない (public-private boundary)。
portable な部分は「stall candidate を『delivered dispatch journal + 期待 durable
journal の欠如』で定義し、四状態に分類し、stall check と再通知を必ず issue に
記録する」ことである。

## サブレーン完了条件と coordinator drain (#12213)

callback (#11852)、停止点標準 (#11860)、stall 検出 (#11880) はそれぞれ
multi-lane 運用の一本ずつの rail を定義する。version #222 で複数サブレーンを
同時運用したところ、これらが暗黙に残していた gap が再発した: durable Redmine
state は進んでいるのに、完了条件が「gate journal が出た」止まりで「coordinator
が見て resume できる」までを含んでいないため、サブレーンでは完了に見えるのに
coordinator からは停止して見える。#12189 / #12190 / #12191 / #12207 の PoC
失敗を再発防止観点で一般化し、handoff-worthy state の完了条件を定義として
締め直す。新しい checker / drain CLI は作らない (それは別段階)。

観測された portable な判断は次である。fixed fields は将来 checker 化しやすい
形に固定する: `state`, `durable_anchor`, `callback_result`, `blocked_by`,
`resume_condition`, `resume_owner`, `origin_reachable`。

- **handoff-worthy state は callback outcome journal まで未完了。** 完了条件は
  「gate journal がある」ではなく「gate journal があり、callback outcome
  journal が coordinator を指している」である。`implementation_done`,
  `review_request`, `review_result`, `owner_close_approval_waiting`, `blocked`
  は callback outcome journal が記録されるまで完了扱いにしない。`callback_result`
  (`sent` / `blocked` / `not-attempted`、沈黙は不可) は完了の構成要素であり、
  後で足す step ではない。自 lane の Codex に通知しただけは `implementation_done`
  の完了ではない (#12189: 実装 / 承認は landed だが callback / downstream
  resume が drain されなかった `progress_without_callback`)。
- **dependency hold は go-ahead 待ちにせず durable parked state を記録して
  止まる。** 他 issue 依存で進めないときは「着手してよいか」と質問で止めず、
  parked state を durable journal に書いて yield する。parked journal は
  `state: blocked`, `blocked_by`, `resume_condition`, `resume_owner`,
  `callback_result` を残す (#12191: 依存 hold が Redmine 記録前に operator
  go-ahead 待ちになった。#12190: `blocked_by #12189` は正しく待機したが resume
  責務が anchored されなかった)。parking は質問でも stall でもなく、durable
  parked state + callback である。resume の trigger は `resume_condition` が
  durable record 上で真になることであり、pane への nudge ではない。
- **coordinator は callback drain / downstream resume の責務を持つ。**
  サブレーンが state を報告するだけでは足りず、callback を消費し、待機していた
  作業を再起動する actor が要る。coordinator lane が callback drain (蓄積した
  callback outcome journal を読み次アクションを取る) と downstream resume
  (parked dependent を `resume_condition` 充足後に再 dispatch する) を owns する
  (#12189 → #12190: #12189 は承認に達したが callback / #12190 の resume が drain
  されず parked のまま放置)。`blocked_by` issue が自分の callback outcome
  journal に達したら、coordinator が `resume_owner` として dependent を再
  dispatch し、resume を dependent issue の routing / Progress Log journal に
  残す。parked サブレーンは polling で self-resume しない。
- **commit hash を gate に記録する前に origin reachability preflight を必須化。**
  `implementation_done` / `review_request` gate に commit hash を書く前に、その
  commit が `origin` から reachable であることを確認し、結果を `origin_reachable`
  として残す。local-only (未 push / rebase で orphan) な hash は reviewer が
  fetch できず Review Gate を block させる (#12207: commit origin reachability
  未達のまま Review Request が出て Review Gate が blocked)。preflight は具体的:
  branch を push し、`git rev-parse HEAD` の hash が `git branch -r --contains`
  / `git ls-remote origin` で `origin/...` ref に乗っていることを確認する。
  reachable でなければ gate は未 ready で hash は記録しない。これは
  `## Audit-Owned Commit Authority` の hash 記録 step の上流の precondition で
  あり、それを置換しない。後続 coordinator rebase が記録済み hash を無効化
  した場合は re-anchoring correction journal で直す (code 変更ではない) が、
  preflight は初回記録が unreachable になることを止める。

これは停止点標準 (#11860) や owner 承認集約 (#11867)、stall 検出 (#11880) を
緩めない。coordinator の drain / resume も owner-gated な次アクションは
owner 承認集約を通す。具体的な drain cadence / resume 優先順は operator runtime
policy であり OSS default に混ぜない (public-private boundary)。portable な部分は
「handoff-worthy state は callback outcome journal まで未完了、dependency hold は
parked state を記録して止まる、coordinator が callback drain / downstream resume
を owns、commit hash は origin reachability を確認してから gate に記録する」を
上記 fixed fields の形で残すことである。

## サブレーン retirement drain (#12214)

#12213 はサブレーンの life の *前半* を締めた: handoff-worthy state は callback
outcome journal まで未完了で、coordinator が callback drain / downstream resume を
owns する。だが life の *後半* — issue close 後の lane 撤収 — は未定義のままだった。
version #222 で複数サブレーンを同時運用したところ、issue が closed になった後も
lane / worktree / cockpit pane が無期限に残存し、1 version に
Redmine-closed だが resident な lane が多数積み上がった (#12214、v0.9.1 サブレーン
PoC の再発防止一般化、#12213 の後続)。これは単なる operator cleanup の怠慢ではなく、
workflow が *retirement* を明示的な完了工程として扱っておらず、誰も owns せず安全化も
していない設計 gap である。本 doc は sublane retirement を **callback drain の次に
走る coordinator-owned drain** として定義する。新しい automated retire CLI / checker
は作らず、既存 lane の実 kill / worktree remove も行わない (#12214 非スコープ)。
fixed fields は将来 checker 化しやすい形に固定する: `retirement_state`, `lane`,
`worktree`, `pane`, `redmine_issue_state`, `retain_reason`, `downstream_consumed`,
`retire_blockers`, `safety_preflight`, `durable_anchor`。

観測された portable な判断は次である。

- **closed lane は default retire candidate。** lane の Redmine issue が closed に
  なったら、その lane は default で `retire_candidate` (worktree / pane が撤収対象)
  になる。ここでの close は durable な close であり、`implementation_done` でも
  Review Gate approval でもない (implementation_done は完了ではない)。
  `implementation_done` / `owner_close_approval_waiting` 止まりの lane は
  retire candidate ではなく、#12213 のとおり in-flight である。candidate 集合は
  durable record (issue state) から導出し、pane scrollback / `status` / `doctor`
  から推測しない。
- **dependency ancestor lane は downstream consumed まで retain。** branch が
  downstream lane の merge / rebase 先になっている ancestor lane は、その消費完了まで
  撤収を保留する。`retirement_state: retain_until_downstream_consumed` を `retain_reason`
  (どの downstream issue / lane が依存しているか) と `downstream_consumed: false` で
  記録する。これは #12213 の dependency hold の retirement 側の鏡である (あちらは
  dependent が `blocked_by` で park、こちらは ancestor が retirement から hold)。
  downstream が ancestor commit に merge / rebase し、その消費が記録されたら
  (`downstream_consumed: true`) hold を解除し candidate に戻す。解除は coordinator が
  owns し、ancestor lane は polling で self-release しない。
- **retire 禁止条件 (どれか open なら `retire_blocked`、`retire_ready` にしない)。**
  active lane / review pending / owner approval pending / unresolved callback /
  dirty worktree / pending prompt / unpushed commit / unknown target identity の
  いずれかが open な間は撤収を禁止する。各 open 条件は `retire_blockers` の entry に
  なる。unresolved callback は #12213 の callback drain 完了で消える。dirty worktree /
  unpushed commit は worktree remove が作業を捨てる危険、pending prompt は pane kill が
  in-flight 入力を落とす危険、unknown target identity は識別できない pane を kill しない
  という destructive 安全条件である。
- **destructive 操作前の safety preflight を必須化。** pane kill / worktree remove の
  前に `safety_preflight` を実行し記録する。全 field が true であることが
  `retire_candidate` を `retirement_state: retire_ready` に進める条件である:
  `redmine_closed` (durable close), `worktree_clean` (`git status` clean),
  `origin_reachable` (lane branch の全 commit が origin から reachable、push 後に
  `git branch -r --contains` 等で確認し worktree remove が work を失わない),
  `pending_prompt_absent`, `callback_drained` (この lane の callback drain 完了),
  `target_identity_known` (pane id / worktree path / branch を durable record /
  resolver から positively 解決)。どれか false なら `retire_blocked` のままで
  destructive 操作は走らせない。
- **retire 前後を journal で bracket する。** 撤収を `retire_ready` (preflight green、
  撤収直前) と `retired` (pane kill / worktree remove 実施済み) の 2 journal で挟む。
  `retire_ready` は `retire_blockers: []`、green な `safety_preflight`、
  `durable_anchor` (#<issue_id> j#<close_journal_id>) を残す。`retired` は
  `retirement_state: retired` と removed/killed の worktree / pane、`durable_anchor`
  (retire_ready journal) を残す。
- **coordinator は callback drain の次に retirement drain を owns。** retirement は
  coordinator の責務で、callback drain の後に sequence する。callback がまだ owed な
  lane を retire すると handoff-worthy state を取りこぼすため、順序が重要である。
  coordinator は closed-issue lane を durable record から列挙し (owner 承認集約 /
  stall 検出と同じ pane-count 非依存の列挙)、`retire_blockers` を clear し、
  `retire_ready` を記録し、destructive 操作を行い、`retired` を記録する。
  `retire_ready` に到達できない lane は `retire_blocked` のまま coordinator の
  backlog になる。retirement は close を self-authorize しない (既に closed な issue の
  lane のみ retire し、retire のために issue を close しない)。

これは停止点標準 (#11860) / owner 承認集約 (#11867) / stall 検出 (#11880) /
#12213 完了条件を緩めない。具体的な retirement cadence / closed-lane 列挙 query /
撤収前 grace window は operator runtime policy であり OSS default に混ぜない
(public-private boundary)。portable な部分は「closed lane は retire candidate、
dependency ancestor は downstream consumed まで retain、open hold 条件は retirement を
禁止、destructive 操作は green safety preflight を要する、coordinator が callback drain
の次に retirement drain を owns」を上記 fixed fields の形で `retire_ready` / `retired`
journal で bracket して残すことである。

## Ticket 化するもの

この PoC では、運用上の friction を意図的に child issue 化する。finding が
具体的で、再発しやすく、独立して修正可能なら ticket にする。

すでに観測された例は次である。

- cockpit append width rebalance。
- dogfooding 中の stale installed CLI。
- Redmine task 作成時の subject / description separation (#11856)。
- Claude pane launch permission mode。
- main unit Claude role boundary。

#11850 は integration record として保つ。独立した fix path が必要な問題を、
構造のない dump として #11850 に積まない。

## Claude pane permission mode (#11857 / #11925)

PoC 運用中、operator が cockpit / sublane の Claude pane を毎回 `Shift+Tab`
で auto mode に切り替え忘れ、multi-sublane dogfooding が停止する friction
が観測された。#11857 は managed Claude pane の launch command に permission
mode を渡せる primitive を実装したが、その付与条件が env var の opt-in だけ
だったため、cockpit session に env が未設定だと bare `claude` で起動し、lane
が停止し得る冪等性 gap が残った (#11924 j#58206)。

#11925 で、この gap を **launch-context policy** として解消した。設定責務は
mozyo の managed pane 作成経路にあり、repo-local の `.claude/settings.json` /
`.claude/settings.local.json` には書かない (Claude Code v2.1.142 以降は
repo-local `defaultMode: "auto"` を無視する設計のため。#11924 j#58207)。

resolution は pure module `src/mozyo_bridge/domain/claude_permission_policy.py`
にあり、launch chokepoint (`_agent_launch_command`) と `doctor` の両方が同じ
precedence を参照する。

- precedence は `env override > launch-context policy default > none`。
  1. `MOZYO_CLAUDE_PERMISSION_MODE=<mode>` (env var) が set されていれば、
     その値が最優先で `--permission-mode <mode>` として付与される。これは
     #11857 の primitive を **互換 / 明示 override rail** として残したもので、
     唯一の正本ではない。`MOZYO_CLAUDE_PERMISSION_MODE=default` のように auto を
     明示的に切る用途にも使える。
  2. env が unset / blank なら、launch-context の policy default が効く。
     cockpit / layout / sublane (cockpit append) の managed Claude pane 作成
     経路は `COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT` (= `auto`) を渡すため、
     env var なしでも future Claude pane は再現可能に `claude --permission-mode
     auto` で起動する。
  3. それ以外 (standalone な `mozyo` window 経路) は policy default を渡さない
     ため、従来どおり bare `claude` で起動する。既存挙動を silent に変えない。
- Codex pane には一切影響しない。flag は Claude 限定で、cockpit default を
  渡しても Codex launch command は変わらない。
- choices は local `claude --help` 由来: `acceptEdits`, `auto`,
  `bypassPermissions`, `default`, `dontAsk`, `plan`。未知の値は launch 時に
  hard error にして、typo が default-permission pane へ silent fallback する
  ことを防ぐ。

### 非 retroactive 性

- CLI の `--permission-mode` flag は mozyo が **新規に作る** pane の launch
  command にのみ効く。既に起動済みの Claude pane には retroactive に効かない。
  既存 pane を auto に変えるには、`Shift+Tab` での手動切替か pane の再起動が
  必要である。policy 変更 (cockpit default 化や env unset) も、その後に作られる
  pane にしか効かない。

### 検出 (dry-run / doctor)

- `mozyo layout --dry-run` / `mozyo cockpit --dry-run` は planned launch
  command を出力するため、future Claude pane が `--permission-mode auto` で
  起動するかをそのまま確認できる。
- `mozyo doctor` の `claude_launch_policy` section は、future cockpit /
  sublane Claude pane の effective permission mode と source (policy default /
  env override) を報告する。auto にならない状態 (env override で auto を切って
  いる / env 値が不正) は warning として surface し、unset policy が lane を
  silent に止めないようにする。

### 安全境界

- CLI の `--permission-mode` flag は、その session 限りで settings.json の
  `permissions.defaultMode` を上書きする。mozyo はこの flag を launch
  command に渡すだけで、user / project local settings file を読み書きしない
  ため、on-disk settings と衝突しない。
- `auto` / `acceptEdits` / `bypassPermissions` / `dontAsk` は autonomy を
  広げる方向の mode である。cockpit / sublane の実装 worker pane を auto で
  起動するのは、bounded な lane で実装を進めるための policy default であり、
  permission mode が広げる autonomy の安全境界は Redmine gate と durable
  record 側に置く (permission mode は実行境界そのものではない)。
- env var はその shell / cockpit session のスコープであり、durable な
  governance state ではない。override rail として使い、恒久 policy の正本とは
  しない。

## Redmine task subject / description separation (#11856)

PoC 運用中、`create_task_tool` に Markdown 長文を渡した際に subject が body の
先頭見出し (`## 背景`) になり、後続で手修正が必要になる friction が観測された
(#11850 j#57294)。subject は body から導出されるべきものではなく、明示の一行
要約である。

これを再発しにくくする運用ルールを distributed skill 本体
(`skills/mozyo-bridge-agent/references/workflow.md` の
`## Ticket System Conventions` > `### Issue Subject / Description Separation`)
に置いた。要点は次である。

- **explicit-subject-on-create**: `create_*` (Redmine `create_task_tool` /
  `create_user_story_tool` 等、Asana task 作成) を呼ぶときは、body と独立した
  簡潔な一行 subject を必ず明示で渡す。Markdown 見出し / body 先頭行 / 切り詰め
  断片を subject にしない。
- **description 分離**: 目的 / 対象 path / 受入条件 / 参照は description に置き、
  subject に流し込まない。
- **即時修正**: 誤 subject (見出し断片 / 切り詰め断片) が land したら、その
  session 内で `update_issue_subject_tool` (Asana は task 名更新) で簡潔な要約に
  直し、durable record (journal / comment) に修正を記録する。後続の手 cleanup に
  残さない。

この規約は creation-time の discipline を足すだけで、gate 語彙 / hierarchy /
必須 field を変えない (それらは central preset 側の正本のまま)。operator 固有の
subject 文体 / 命名テンプレートは runbook 側 (`public-private-boundary.md`) に置き、
distributed body には焼かない。

### MCP tool boundary finding (#11885)

`create_task_tool` に `subject` 引数を足す要求 (#11885) を調査した結果、`subject`
未対応は **外部接続 MCP server `redmine_epic_grid` 側の schema 非対称**であり、
`mozyo_bridge` repo 内の実装ではないと確定した。現セッションの tool schema は次:

- explicit `subject` あり: `create_epic_tool` / `create_feature_tool` /
  `create_user_story_tool` / `create_inquiry_tool`。
- `subject` 無し (`description` の先頭内容から subject 導出): `create_task_tool` /
  `create_bug_tool` / `create_test_tool`。#11884 の `## 背景` subject はこの導出が原因。

したがって #11885 受入条件のうち「`create_task_tool` で subject を明示指定できる」は
本 repo では満たせない。正しい実装先は外部 `redmine_epic_grid` MCP server で、
上記 3 leaf creator に `subject` field を追加し container 系 creator と対称化する
upstream 変更である。本 repo 側の対応は、leaf creator 向けの運用 mitigation を
distributed skill (`skills/mozyo-bridge-agent/references/workflow.md` の
`### Issue Subject / Description Separation`) に明記することに留める: description 先頭行を
heading marker 無しの一行 plain-text summary にし、生成後に subject を検証して
誤ったら `update_issue_subject_tool` で即修正する。

## Revision Principle

この文書は、観測された workflow risk と現時点の operating judgment を記録
するものである。特定 model の品質に関する恒久的な主張ではない。

Claude と Codex の挙動は時間とともに変わる。tool が変わったらこの文書も
見直す。ただし、より安全で単純な model が存在する証拠がない限り、次の
core separation は維持する。

- durable state は Redmine に置く。
- identity は workspace / lane / pane level で扱う。
- boundary を越える routing は Codex gateway を通す。
- implementation は bounded lane で行う。
- owner-facing decision は coordinator lane で行う。
