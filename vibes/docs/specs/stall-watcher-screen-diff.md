# Stall Watcher — 画面差分を一次センサーにした停滞検知と処方 (Redmine #15843)

## Status

- version: `v0.1`
- scope: watcher 層 (background / operator) が、pane の**描画画面が進んでいるか**だけを一次
  センサーとして停滞候補を拾い、種別を分類し、種別ごとの処方を**提示**するまで。
- non-goal: 処方の自動適用、配送 rail の retry policy、completion 判定、receiver-state
  observability の再設計。いずれも既存正本が所有する (`## 既存正本との境界`)。
- 実装: `e_110_execution_platform/f_150_runtime_observation_event_timeline` の
  `domain/pane_stall_sensor.py` / `domain/stall_disposition.py` /
  `application/stall_watch_pass.py` / `application/cli_workflow_stall_watch.py`、
  provider data は `e_140_adapter_provider/f_160_provider_registry/domain/agent_provider_stall_signatures.yaml`。

## なぜ画面差分なのか (この class に他のセンサーが無い)

provider の server が応答を止めると、model は turn を返さない。したがって **ACK も
callback も durable journal も runtime event も一切発生しない**。この repo が持つ検出器は
すべて「動いている provider が出す signal」に anchor しているので、この class では全部が
永久に沈黙する。

2026-08-21 夜の 3 件の停滞 (#15841 silent stall / #15789 cyber-block 残骸 / #15842 update
prompt) は、いずれも **owner が pane を目視したことでしか発見されなかった**。issue #15843
description の owner 発案はこの実測の読みであり、本 spec の設計前提である:

> 一番防げないのは provider のサーバーダウンで応答が止まる類。…崇高な仕組みは壊れやすいので、
> 汚い原始的な実装 (画面差分) の方が robust。

「画面が変わっているか」がこの class の唯一の地の真実であり、provider 固有の health parse
より頑健である、という判断をそのまま採る。

## 一次トリガーであって判定ではない

`vibes/docs/logics/ack-completion-receiver-state.md` の
`## なぜ pane text / stdout silence を completion truth に昇格させてはいけないか` は、pane の
沈黙を completion / liveness の verdict へ昇格させることを禁じている。その理由は本 spec でも
そのまま効く: **沈黙は reasoning 中・permission 待ち・長い test run の姿でもある**。

したがってセンサーは「2 sample の間に描画画面が動いたか」だけを答え、**停滞と結論しない /
処方を選ばない / 何も送らない**。結論は分類器が出し、処方は分類の後にしか存在しない。

## センサー: 3 値であって boolean ではない

`changed / unchanged` の boolean は実 TUI に対して両方向に空虚である。

- 生きている TUI は idle でも animate する (spinner、経過秒、token counter)。byte 一致は
  ほぼ成立しないので、素朴な「無変化」トリガーは**一度も発火しない**。
- 類似度 threshold だけだと「animation は動いている」と「描画ループごと止まった」が同じ値に
  潰れる。分類器が最も必要とする区別がここで失われる。

そこで **animate している chrome 自体が liveness signal である**ことを使い、3 値にする。

| 状態 | 定義 | 意味 |
| --- | --- | --- |
| `changed` | 類似度が threshold 未満 | 内容が進んだ。停滞候補ではない |
| `chrome_only` | threshold 以上だが完全一致ではない | 描画ループは生きていて内容だけ止まっている = 正当 busy の姿 |
| `identical` | 正規化後に byte 一致 | animation すら進んでいない。強い signal |
| `incomparable` | 片方でも読めない | どちらの証拠でもない |

この区別があるので、**サンプリング間隔を長く取る必要がない**。停滞と busy を分けるのは
持続時間ではなく chrome が動くかどうかである。既定間隔は bounded-wait watcher の 1 tick
(50 秒) で足り、owner 発案の「1 分」を閾値として採らないのはこの理由による。持続時間は
escalation では意味を持つが、それは durable record の問題であって 2 sample の性質ではない。

正規化は行末空白と末尾空行だけを落とす。spinner の桁位置のような **provider 固有領域を
strip し始めた時点で、owner が退けた「壊れやすい provider 固有 parse」に戻る**ため、それは
しない。

### 既知の calibration 限界と、その倒れる向き

類似度は画面全体で取るので、同じ counter の 1 tick は「ほぼ空の pane」では大きな相対変化に、
「文字で埋まった pane」では無視できる変化になる。したがって短い画面は `chrome_only` ではなく
`changed` に落ちうる。**この向きは意図的に安全側**である: 小さい画面では under-trigger
(進捗と報告し、何も処方しない) になり、busy を frozen と誤読する逆向きにはならない。
threshold を定数ではなく引数にしているのはこのためである。

## 分類: 順序と、交差の明示

非進行の画面だけを分類する。first-match だが、**交差する組は順序に押し付けず名指しする**。

| # | 条件 | class |
| --- | --- | --- |
| 1 | `incomparable` | `screen_unreadable` |
| 2 | `changed` | `screen_progressing` |
| 3 | 読めたが空白のみ | `unknown` |
| 4 | 宣言済み startup screen に一致 | `startup_interaction` |
| 5 | 投入済み body が画面に残存 | `unsent_composer` |
| 6 | 宣言済み stall signature に一致 | signature が宣言する class |
| 7 | `chrome_only` | `busy_likely` |
| 8 | `identical` | `unresponsive_indeterminate` |

rule 7 と 8 が非進行の 2 状態を尽くすので、到達不能な末尾は無い。

交差 (両条件が同時に成立しうる組) と precedence:

| 交差 | 勝つ側 | precedence_basis |
| --- | --- | --- |
| 4 × 5 | 4 | `least_effect_first` — startup screen 上の Enter は dialog の既定を選ぶ。それが #13760 / #14741 の defect 本体なので、Enter 処方は startup screen が出ている間 到達可能であってはならない |
| 4 × 6 | 4 | `role_precedence` — rendered-confirmed 証拠 + operator 所有の処方が、低 tier の suspicion に優先する |
| 5 × 6 | 5 | `direct_evidence_over_suspicion` — body 残存は**この dispatch** についての観測、banner は provider についての推論。処方 (Enter 1 回、本文再入力なし) は ADR-0002 の bounded budget でいずれにせよ許される |

`unknown` は fail-safe として実在し、rule 3 で到達する。「読めたが空の画面」を frozen と
呼ぶことは、read についての所見を lane についての所見に見せかけることになる。

## 処方: 検知 ≠ 回復

処方が相互に破壊的であることが、分類を必須にしている理由である。

| class | 処方 | 根拠 |
| --- | --- | --- |
| `screen_progressing` / `busy_likely` / `screen_unreadable` / `unknown` | `no_action` | 沈黙は停滞ではない (ack-completion-receiver-state) |
| `startup_interaction` | `operator_resolves_startup_screen` | screen を宣言することは答えることを authorise しない (#13760 / #14741 境界) |
| `content_refusal` | `context_reset_reinjection` | #15816 の 1 回だけの切り分け。loop にしない |
| `unsent_composer` | `enter_only_retry` | ADR-0002 / #15842。本文は再入力しない |
| `provider_unresponsive_suspected` | `patient_wait_then_retry` | #15843 owner intent。relaunch しない |
| `unresponsive_indeterminate` | `patient_wait_then_retry` | 同上 |

**posture は全件 `present_only`** である。watcher は報告し、権限を持つ actor が決める。
自動適用の余地を flag ではなく**面の不在**として表現しているので、watcher に実行権を与える
ことは既定の反転ではなく、この層への reviewable な変更になる。

### server-down と frozen を分けない、という設計判断

外から見て両者は区別できない (どちらも画面が凍り、何も emit しない)。誤りのコストは著しく
非対称である:

- wedge した lane を待つ = 時間を失う
- server-down の lane を relaunch する = **作業を失う**

したがって両者は `unresponsive_indeterminate` に**意図的に統合**し、patience を共有処方と
する。relaunch は本層の最初の答えにも自動の答えにもならない。**caller が durable record から
「patient window は既に使い切った」と表明した場合にのみ**、`owner_escalation` の
*候補* として人間に提示される。busy / 進行中 / 読めなかった unit は時間経過だけでは
escalate しない (長い test run が relaunch 候補に育たない、という機械的保証)。

## Provider signature データと evidence tier

provider 固有文字列は 1 つも source に置かない。すべて
`agent_provider_stall_signatures.yaml` にあり、各 entry は
`startup_blockers` と同じく **1 画面上に共在する部分文字列の AND** である。

`agent_provider_profiles.yaml` の field にしないのは line 数の都合ではなく境界の問題である:
あちらは **pre-send zero-send 拒否 gate** (#13760) が読む安全面で、これは **何も送らない
watcher** が読む面であり、後者は evidence tier が弱い。2 つの registry を分けておくことが、
弱い tier が送信境界の拒否 gate を広げられないことの機械的保証になる。

| tier | 意味 | 主張できる class |
| --- | --- | --- |
| `rendered_confirmed` | shipped binary から読み、**実際に描画して確認**した (#14741 の基準) | 宣言済みの任意 class |
| `binary_read_unrendered` | shipped binary から読んだが、再現に上流障害が要るため描画未確認 | `provider_unresponsive_suspected` のみ |

後者の安全性の全体は次の 1 点に帰着する: **その class の処方は、signature が 1 つも一致
しなかった場合に既に得られる処方と同一である**。したがってこの tier の literal が誤って
いても、変わるのは *報告される理由*であって *推奨される行動* ではない。schema はこの制約を
load 時に強制し、packaged artifact 自体も同じ不変条件で検査される。

`unsent_composer` は **どの tier でも宣言不可**である。これは投入 body に対する composer
証拠 (#15842) で成立する class であり、data file が部分文字列で主張できてしまえば #15842 が
除去した推測がそのまま戻る。

### 記録済み残余: `content_refusal` の signature は未宣言

#15789 j#109183 は Codex session が依頼を cybersecurity 隣接として拒否し作業に入らなかった
ことを記録し、#15816 が回復をドクトリン化した。したがって **class は実在する**。しかし
その画面の**描画文言は捕捉されていない** (#15789 の記録は挙動であって literal ではない) し、
両 provider の shipped binary を探しても user 向けの refusal banner を pin できなかった。

記憶から文字列を書くことは、この schema 系列が明示的に禁じている
(`agent_provider_profiles.yaml`:「read from the shipped binary, not from memory」)。しかも
`content_refusal` の誤判定は、この spec で唯一実害のある誤りである — その処方は生きた
session の context を捨てる。したがって class は宣言可能なまま **data は空**とし、未一致の
refusal 画面は `unresponsive_indeterminate` → patience に落ちる。**間違っているが無害**で
あって、**正しいが破壊的**ではない側を選んでいる。

追加の条件は #14741 が Codex の update prompt を宣言する前に満たしたものと同じである:
画面を観測し、literal を shipped binary から読み、描画して確認する。それまでは、この不在は
見落としではなく記録された残余である (unit test が assertion として保持している)。

## 置き場: watcher 層であって LLM turn ではない

skill `references/workflow.md` の `## Wait / polling 効率標準` は、dispatch 後に LLM turn が
待つことを禁じ (zero-wait)、bounded wait の置き場を background watcher と operator debug へ
移している。本 pass は sampling 間隔だけ sleep するので、**watchdog process または operator
のものであり、agent が自分の dispatch を poll するために呼ぶものではない**。CLI の help に
その旨を書いてあるのは、`--help` を読む agent が最初に見る場所だからである。

N 個の target に対して interval は **pass ごとに 1 回**しか sleep しない (target ごとではない)。
target ごとに待つと cockpit の成長に対して cadence が線形に劣化し、それは手動 poll を破綻
させたのと同じ性質である。

## 既存資産の再利用 (新規 primitive を作っていないもの)

- **画面の read**: caller が束ねた `TerminalTransportPort.read_pane` (herdr `agent read
  --source visible`)。send 経路が既に信頼している read-only primitive をそのまま使い、
  argv / payload 解析 / timeout を 1 箇所に保つ。
- **startup screen の分類**: `evaluate_startup_admission` (#13760)。provider ごとの宣言済み
  blocker を既に知っており、未 profile provider については既に推測を拒否し、pane text を
  既に返さない。
- **body 残存**: #15842 の submit-proof の考え方。ただし screen の部分文字列を data に
  発明させるのではなく、caller が durable delivery record から渡した marker と照合する。
- **回復ドクトリン**: #15816 (context reset) / ADR-0002 + #15842 (Enter-only) / #13760 /
  #14741 (screen は operator が解く)。本 spec はこれらを分類の**帰結**として参照するだけで、
  いずれの挙動も再定義しない。

## 出力の hygiene

観測結果は **pane content を一切運ばない**。`StartupAdmission` と同じ規律で、画面が「何に
一致したか」は固定 token (`matched_id`)、画面が「何と書いてあったか」は分類器の外に出ない。
これにより 1 pass の JSON envelope はそのまま durable journal に貼れる。

## 既存正本との境界 (本 spec が緩めないもの)

- **durable record が正本であり続ける**。本 watcher は停滞候補を見つけて分類するだけで、
  completion / gate / close のいずれも判定しない (`ack-completion-receiver-state.md`)。
- **stall candidate の定義を置き換えない**。skill `## Stall / no-progress 検出標準` は stall
  candidate を「delivered な dispatch journal + 欠落した期待 durable journal」と定義する。
  本 watcher はその補完層であり、pane を trigger の正本に昇格させない — 本 watcher が出力
  するのも「durable record を読むべき候補」である。
- **配送 rail の retry policy に触れない**。ADR-0002 / #15842 / #15537 の Enter budget、
  composer clear 要件、fail-closed 語彙はいずれも本 spec の対象外である。
- **raw pane mutation を通常経路にしない**。本層は read 専用であり、処方は提示に留まる
  (ADR-0013 の「ユーザーに pane 操作を意識させない」目標状態に対しても、手動操作を新たに
  既定化しない)。
- **operator 固有の policy を OSS default に入れない**。どの target を、どの cadence で、
  どれだけ待ってから escalate するかは operator の runtime policy である。portable なのは
  *3 値センサー / 分類の順序と交差 / 処方 map / patience が fail-safe であること / evidence
  tier* までである。

## Cross-References

- `vibes/docs/logics/ack-completion-receiver-state.md` — 沈黙を completion にしない正本、
  および #15842 の「起動 busy と処理 busy は event で区別できない」節
- `vibes/docs/logics/tmux-send-safety-contract.md` — 送信側 rail の挙動正本
- `vibes/docs/adr/adr-0002-enter-resend-priority.md` — Enter-only retry の owner 決定
- `vibes/docs/adr/adr-0013-ui-hides-pane-operations.md` — 手動 pane 操作を既定にしない UX 要件
- `skills/mozyo-bridge-agent/references/workflow.md` — `## Wait / polling 効率標準` /
  `## Stall / no-progress 検出標準` / `## 停滞・拒否からの context reset 回復`
