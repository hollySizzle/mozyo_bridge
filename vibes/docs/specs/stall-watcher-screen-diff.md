# Stall Watcher — 画面差分を一次センサーにした停滞検知と処方 (Redmine #15843)

## Status

- version: `v0.9` (#15855 運用配線 + j#110132 / j#110146 / j#110169 / j#110183 / j#110192 / j#110218 / j#110254 review の指摘を反映。v0.1 のセンサー / 分類 / 処方の記述は不変)
- scope: watcher 層 (background / operator) が、pane の**描画画面が進んでいるか**だけを一次
  センサーとして停滞候補を拾い、種別を分類し、種別ごとの処方を**提示**するまで。
- non-goal: 処方の自動適用、配送 rail の retry policy、completion 判定、receiver-state
  observability の再設計。いずれも既存正本が所有する (`## 既存正本との境界`)。
- v0.2 追加 scope (#15855): 上記 pass を**誰が回すか**と、本物の停滞を**どうやって
  coordinator へ戻すか**の配線 (`## 運用配線`)。センサー・分類・処方の意味論は一切変えない。
- 実装: `e_110_execution_platform/f_150_runtime_observation_event_timeline` の
  `domain/pane_stall_sensor.py` / `domain/stall_disposition.py` /
  `application/stall_watch_pass.py` / `application/cli_workflow_stall_watch.py`、
  provider data は `e_140_adapter_provider/f_160_provider_registry/domain/agent_provider_stall_signatures.yaml`。
- 運用配線の実装 (#15855): 同 feature の `domain/stall_escalation_policy.py` /
  `domain/stall_escalation_note.py` / `domain/stall_watch_policy.py` /
  `application/stall_escalation_pass.py` / `application/stall_watch_phase.py` /
  `application/stall_watch_leg.py`、durable state は `core/state/stall_escalation.py`。

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
| 5 | 投入済み body が**現在の composer** に残存 | `unsent_composer` |
| 6 | 宣言済み stall signature に一致 | signature が宣言する class |
| 7 | `chrome_only` | `busy_likely` |
| 8 | `identical` | `unresponsive_indeterminate` |

rule 7 と 8 が非進行の 2 状態を尽くすので、到達不能な末尾は無い。

交差 (両条件が同時に成立しうる組) と precedence:

| 交差 | 勝つ側 | precedence_basis |
| --- | --- | --- |
| 4 × 5 | 4 | `least_effect_first` — startup screen 上の Enter は dialog の既定を選ぶ。それが #13760 / #14741 の defect 本体なので、Enter 処方は startup screen が出ている間 到達可能であってはならない |
| 4 × 6 | 4 | `role_precedence` — rendered-confirmed 証拠 + operator 所有の処方が、低 tier の suspicion に優先する |
| 5 × 6 | 5 | `direct_evidence_over_suspicion` — current composer に残る body は**この dispatch の submit** についての観測、banner は provider についての推論。処方 (Enter 1 回、本文再入力なし) は ADR-0002 の bounded budget でいずれにせよ許される |

rule 5 の証拠は **current composer に限る** (`current_composer_retains_body`、queue-enter retry gate と同一 predicate)。可視画面全体の substring にしてはならない — `ack-completion-receiver-state.md` が「scrollback 全体の substring は使わない」と禁じており、その理由が最も強く効くのがここである: **submit に成功した body ほど transcript に user message として残る**ので、全画面照合は「実際に submit できた pane」を最も熱心に `unsent_composer` と誤分類する。誤分類の帰結は Enter の提示であり、operator を実際の停滞原因から遠ざける。初版はこの誤りを持っており、review (#15843 j#109937) が再現付きで指摘した。上の precedence は証拠が current-composer であることに依存しており、全画面照合の下では成立しない。

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
| `rendered_confirmed` | その文字列が**実画面に出たことが確認され**、その観測が durable anchor に記録されている | 宣言済みの任意 class |
| `binary_read_unrendered` | shipped binary から読んだが、再現に上流障害が要るため描画未確認 | `provider_unresponsive_suspected` のみ |

`rendered_confirmed` の要件は「実画面に出たことの確認」の一点である。#14741 は「binary から読み **かつ** 描画確認」と表現したが、そこで両方が要ったのは *binary から文字列を提案していた*からであり、binary read は候補を得る手段であって信頼性の源ではない。live capture は同じ要件を直接、より強く満たす。到達経路 (binary → 描画確認 / live capture) は entry ごとに data file の comment へ記録する。

後者の安全性の全体は次の 1 点に帰着する: **その class の処方は、signature が 1 つも一致
しなかった場合に既に得られる処方と同一である**。したがってこの tier の literal が誤って
いても、変わるのは *報告される理由*であって *推奨される行動* ではない。schema はこの制約を
load 時に強制し、packaged artifact 自体も同じ不変条件で検査される。

`unsent_composer` は **どの tier でも宣言不可**である。これは投入 body に対する composer
証拠 (#15842) で成立する class であり、data file が部分文字列で主張できてしまえば #15842 が
除去した推測がそのまま戻る。

### `content_refusal`: Codex は宣言済み、Claude は記録済み残余

Codex の signature は **#15789 j#109183 の live capture** から宣言されている。同 journal は
read-only pane debug read で描画画面を逐語記録しており (`This content can't be shown` /
`We take extra caution with cybersecurity requests.` /
`apply for Trusted Access`)、#15789 j#109186 が「同一依頼が 1 回の context reset 後に通った」
= 原因は内容ではなく累積 session 文脈、という disposition を確定している。この class が
escalation ではなく `context_reset_reinjection` (#15816) を処方するのはそのためである。

substring は捕捉された render から、**1 行に収まり幅変化に耐える**断片を選ぶ。実測 render は
pane 幅で hard wrap しており (`If you're a security` / `professional, ...`)、wrap を跨ぐ
substring は幅依存で壊れる。また `This content can` は **apostrophe の手前で止めている**:
capture は `can't` だが、ASCII `'` と typographic `’` のどちらが描画されたかは journal 転記
から確定できないため、確定できる範囲だけを採る (1 文字を推測で足すより狭く採る)。

**Claude 側は未宣言のまま**である。この repo のどの記録も Claude pane の content-policy
refusal の描画を持たず、shipped 2.1.220 binary を探しても内部 wire token と model-card 散文
しか出ず、user 向け banner は pin できなかった。記憶から書くことは schema 系列が禁じており
(`agent_provider_profiles.yaml`:「read from the shipped binary, not from memory」)、しかも
`content_refusal` の誤判定は、この spec で唯一実害のある誤りである — その処方は生きた
session の context を捨てる。したがって Claude については class は宣言可能なまま **data は
空**とし、未一致の refusal 画面は `unresponsive_indeterminate` → patience に落ちる。
**間違っているが無害**であって、**正しいが破壊的**ではない側を選んでいる。これは
#15816 j#109281 / j#109286 が「存在しない観測を補完で書き足さない」を実行し review が受理した
のと同じ形である。追加の条件は上の tier どおり、画面を観測し durable anchor に記録すること。

この残余の由来は残しておく価値がある: Codex 側も初版では同じ理由 (「文言は捕捉されていない」)
で未宣言にしていた。実際には捕捉されており、それは implementation request が指していた
durable record の中にあった。**literal が観測されていないと結論する前に durable record を
探すこと。binary は 2 番目に見る場所であって最初ではない** (#15843 j#109937 の指摘、
j#109938 の verdict)。

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
- **body 残存**: queue-enter retry gate と**同一の** `current_composer_retains_body`
  (`turn_start_resend_gate`)。自前の composer 検出を書かない — それは #15842 が除去した
  推測を別の形で戻すことになる。照合対象の marker は、screen の部分文字列を data に
  発明させるのではなく caller が durable delivery record から渡す。
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

## 運用配線 (#15855)

v0.1 は「センサーと分類器は在るが、誰も回していない」状態で close した。本節はその運用
完成 (operational completion) を記述する。**判断の内容は 1 つも変えていない** — 変えたのは
「いつ動くか」「何を見るか」「何処に書くか」の 3 点だけである。

### OS registration は増やさない

#15192 は「host あたり OS registration はちょうど 1 つ」を確定し、**2 つ目を能動的に削除
する一方向 migration** を出荷している (`supervisor_launchd_migration.py`)。したがって
stall-watch 専用の timer / LaunchAgent は作らない。watcher は既存の 1 unit が回す bounded
sweep の **1 leg** として畳み込む (retire / hibernate leg と同型)。install / uninstall /
restart / service-status は既存 lifecycle をそのまま継承する。

裁定の正本: #15855 j#110121-1。

### 約5分は OS tick ではなく watcher 自身の watermark

OS tick は `DEFAULT_OS_TICK_INTERVAL_SECONDS` (180s) のまま据え置く。callback supervisor の
局所 cadence を劣化させないためである。約5分の周期は `stall_watch_watermark` という
**この watcher 専用の watermark** から出る。provider reconciliation watermark と既定値が
同じ 300s であっても、state / key / 責務は分離する — 一方の変更が他方を黙って再調整して
しまうからである。

phase は tick が回るときにしか走らないので、**実効周期は tick に量子化され 300 秒ちょうどに
はならない**。status は `next_due_at` を「越えるべき閾値」として出し、「次の実行時刻」とは
名乗らない。

### scope は opt-in。設定不在は「何も見ない」

`## 既存正本との境界` のとおり、cadence / N / 対象集合は operator runtime policy である。
これを `.mozyo-bridge/config.yaml` の `stall_watch` block として型付けした。

- **block が無い → 何も見ない。** 「既定値で全部見る」ではない。host 上の全 pane を黙って
  読む watcher は誰も頼んでいない監視面であり、宣言していない lane について escalate する
  のは operator が予期しようのない noise だからである。
- **wildcard は無い。** 全 managed lane を見ることは表現できるが、`all_managed_lanes` という
  operator が自分で打った key としてのみである。cockpit が育ったとき黙って広がる pattern に
  はしない。
- **malformed → 無効 + 理由。** 既定値へ repair しない。cadence を書いて打ち間違えた operator
  が受け取るべきは「選んでいない cadence で黙って動く watcher」ではなく「なぜ止まっているか
  を言う watcher」である。

off の状態は 3 つ (`absent` / `declared_without_scope` / `invalid`) あり、status で区別できる。

### target 発見は scan ではなく join

live agent 行は *候補* にすぎない。4 つの独立した filter を全部通ったものだけが観測対象に
なる: (1) managed identity (`herdr_inventory` に委譲、自前で parse し直さない)、(2) 自
workspace、(3) 宣言済み scope、(4) live generation と authoritative active issue anchor の
**両方**が解決すること。

filter 4 は意図的な死角である。issue anchor が解けない lane について本 watcher は永久に
escalate しない。代替は「どの issue の停滞か推測して coordinator 向け記録を誤った issue に
書く」ことであり、そちらが遥かに悪い。死角は隠さない: 落ちた候補は理由別に数えられ、status
が「N 台が watcher の射程外、内訳はこれ」と出す。

### generation は画面の証拠ではなく権威の事実として先に処理する

generation は `LaneLifecycleStore` という durable な権威記録から来る。画面から推論した値では
ない。したがって **generation の変化は、観測 class の効果を見るより前に決着させる**。

後回しにすると次が起きる (j#110146 finding_3 の実測): relaunch 直後に新 process の画面が読めない
と、HOLD 分岐が generation 比較より先に返るため**死んだ process の run がそのまま残り、しかも
その latch が新 process の escalation を抑制する**。旧 run を破棄するのは「HOLD が reset として
振る舞う」ことではない — 別 process についての記録を退役させているだけで、現在の観測はその後で
空の状態に適用され、HOLD はそこでも advance にも reset にもならない。

### streak は slot に束縛する。locator ではない

run の identity は `workspace_id + lane_id + role` (durable slot) で、terminal `generation` は
**束縛**される (key ではない)。pane locator は evidence として持つだけで、いかなる比較にも
使わない。

- locator は**再利用される**。死んだ unit の locator に貯めた run は、次にそこへ来た別 unit を
  数え始め、止まっていない lane について escalate する。
- locator は同じ unit のまま**変わる**。relaunch / rebind で logical agent は同じまま locator が
  動くので、locator を key にすると run が黙って reset し、rebind を跨いで固まった unit は
  永久に閾値へ届かない。

generation を key に入れると relaunch のたびに孤児行が残るので、key ではなく束縛にする。
generation が変われば run は restart する — 新しい process は自分の画面を持つのであり、
前任者の停滞で新 agent を escalate してはならない。

### 閉じた語彙は「宣言」ではなく「境界での強制」で持つ

operator surface へ出る値の語彙を docstring で宣言しても、**write / read の両境界で検証しなければ
宣言していないのと同じ**。j#110169 finding_1 の実測では、coverage の `dropped` に任意の path
文字列と負数を渡すと `--status` までそのまま通った — 同じ file が「fixed classification token /
count だけなので verbatim に render して安全」と宣言していたにもかかわらず。

したがって discovery の coverage は次を両境界で強制する:

- reason は宣言済み集合の token のみ。語彙外は拒否し、**拒否の message にその文字列を引用しない**
  (引用したら封じ込めたい文字列がそのまま log へ出る)。
- count は非負 integer (`bool` は count ではない)。
- count 同士が整合する: `candidates == watched + sum(dropped)` と
  `out_of_reach == sum(dropped) - foreign_workspace`。producer は候補を必ずどれか 1 つの bucket へ
  分けるので、この 2 本は構成上成立する。破れている row は本 rail が書いた row ではない。

**timestamp も「閉じた語彙」の一部である**。「timestamp だから安全」は、それが timestamp である
ことを誰も検証していない限り成り立たない — 検証していなければ caller 由来の任意文字列である
(j#110183 finding_1 の実測: `/private/example/unsafe-observed-at` が text と JSON の両 status へ
到達した)。宣言する文法は「非空・長さ上限・`fromisoformat` で parse 可能・**tz-aware**」で、
write 時は正規形へ**正規化**して保存する (「parse できるが変な書き方」の余地も閉じる)。

同じ規律を、この store が render するすべての timestamp 列に適用する — discovery の
`observed_at`、pending の `first_observed_at` / `escalated_at` (これらは **Redmine journal 本文**へ
verbatim に入るので exposure がより強い)、watermark の `last_pass_at` (`--status` の `last=`)。
**1 列だけ直して残りを次 round の finding にしない。**

read 側の倒し方は面によって変える: discovery と watermark は値を捨てて typed token に倒せるが、
pending row は**実際の escalation そのもの**なので row を落とさず timestamp だけを閉じた token へ
置換する (落とすと停滞報告が失われる)。

> **v0.6 の誤りの訂正**: v0.6 はここで「SQL の `ORDER BY escalated_at` も影響を受けない」と書いた。
> これは**誤り**である。`ORDER BY` は**保存されている生テキスト**を並べるので、壊れた値は文字列
> 順序で任意の位置に入りうる。read 側で値を token へ倒しても、その row が backlog の**先頭**に
> 居座れば oldest-first という公平性契約が壊れる (j#110192 finding_2)。順序の**権威**は検証済み
> instant から read 側で再導出する。SQL の `ORDER BY` は決定的な**基準**でしかない。

なお `x or default` は「空文字を既定値で黙って修復する」ので使わない。明示的に渡された空文字は
caller の誤りであり、修復ではなく拒否が正しい。

**store は信頼境界ではない**。古い build・手編集・書きかけの row はいずれもこの build の契約を
破る値を保持しうるし、`--status` に実際に流れるのは read 経路である。read 側で検証に落ちた row は
**保存値を 1 つも echo せず** typed な `unreadable` token に倒す (timestamp も echo しない —
reason が信用できない row は timestamp も信用できない)。`null` (= まだ 1 度も走っていない) とは
区別する。operator の次の action が違うからである。

なお store 側の語彙は discovery 層から import せず二重宣言する (state store が方針 module へ
手を伸ばせると、そこに規則が書かれ始める)。二重化が安全なのは**双方向の一致を test が機械照合
する**場合だけである。

### operator surface へ出す文字列は閉じた語彙だけ

policy の `detail` は `--status --json` に出る。**raw な例外文字列を載せてはいけない** — YAML
parse 失敗の message は config の絶対 path と file 本文の断片を両方含む (j#110146 finding_2 の
実測)。切り詰めは redaction ではない。載せるのは例外の型名と、**本 block 自身の validator の
場合に限り**宣言済みキー名との完全一致 token だけにする。

分類も文字列一致でやらない。「本 block が malformed」か「別の理由で読めない」かは **exception
chain の型** (`__cause__` を辿って自 validator の error 型を見つけるか) で決める。message に
`stall_watch` が含まれるかで判定すると、`stall_watch_extra` のような **sibling key を本 block の
malformed と誤報する**。

### operator が読める runtime status

宣言した設定は `config status` が出す。**動いているかどうか**は別の面が要る。既存の
`workflow supervisor --status` に runtime readback を足した (新 surface も新 gate も作らない):

- policy の `enabled` と reason (`absent` / `declared_without_scope` / `invalid` /
  `config_unreadable` の 4 状態を区別する。malformed を absent に潰すと、cadence を打ち間違えた
  operator に「一度も設定していない」と答えることになる)、cadence と threshold の実効値;
- `last_pass_at` と `next_due_at` (後者は閾値であって予定時刻ではない);
- `out_of_reach` — live だが watcher の射程外の unit 数。**status が discovery を再実行して求める
  のではなく、leg が各 pass で永続化した集計を観測時刻つきで projection する** — status が pane を
  読む command になってしまうのを避けるため。記録が無い状態 (`null`) は「まだ 1 度も走っていない」
  であり「走ったが 0 台だった」とは区別する;
- pending の内訳 `unrecorded` / `anchorless` / `recorded_but_unwoken` と
  **`oldest_unrecorded_age_seconds`** — これが飢餓の可視化そのもの。

sweep 側は leg の outcome を捨てず `SupervisorReport.stall_watch` に載せる。leg が例外を投げた
場合も `leg_error` + 例外の**型のみ**を記録する (message は path や画面を引用しうるので載せない)。
「watcher が壊れている」と「watcher に見るものが無かった」が operator から同じに見えてはいけない。

### 発火条件: 同一 class の N 連続。無evidence は数えない

| class | 効果 |
| --- | --- |
| `screen_progressing` / `busy_likely` | **reset** (render loop が生きている積極証拠) |
| `screen_unreadable` / `unknown` | **hold** (どちら向きの証拠でもない) |
| `startup_interaction` / `content_refusal` / `unsent_composer` / `provider_unresponsive_suspected` / `unresponsive_indeterminate` | **advance** |

`hold` は進めも戻しもしない。進めれば「読めなかった *reader*」が、誰にも見えない unit に
ついて停滞の verdict を捏造できてしまう。戻せば 1 回の読み取り失敗が 5 pass 分の本物の run を
消してしまう。機械的な帰結として、**ずっと読めない target は決して閾値に届かない**。

`startup_interaction` を advance 側に置くのは、trust dialog に座った unit が「遅い」のではなく
**永久に止まっている**からである。処方 (`operator_resolves_startup_screen`) 自体が「人間だけが
解ける」と既に言っている。

閾値に達した run は **1 回だけ**発火して latch する。長時間の停滞が 5 分ごとに人を呼ぶことは
ない。その代償は明示された残余である: **本層は未確認の escalation を再送しない**。cadence で
再発火する watcher は mute されるものになり、mute された watcher は無いより悪い。落ちた
escalation の回収は durable record 側の責務であり、本層が完了を推測してよい事柄ではない
(ADR-0014)。

### durable record の正本は Redmine journal

local SQLite は **streak と pending の durability** であって workflow truth ではない。閾値到達は
既存語彙の `## Gate: blocked` (`reason: stall_watch_escalation`) journal を canonical gate
writer 経由で append することで記録する。新しい gate token も新しい transport kind も作らない
— `blocked` は既に「coordinator を起こして journal を読ませるだけで、何も authorize しない」と
定義されており、本 rail の意味そのものである。

note は固定 field のみを載せ、**pane content を構造的に運べない** (renderer に画面文字列を
渡す引数が存在しない)。note は観測を主張し、結論は主張しない — unit が死んだとも、作業が
完了したとも、処方が適用されたとも書かない。`policy` field は cadence / N / 出所を載せるので、
「N 連続」が後の読者にとって意味を持つ。

### 発火 class の evidence は配送履歴から join する

`unsent_composer` は「dispatch した本文が composer に残っている」ときにだけ成立する。marker を
渡さなければこの class は**定期実行経路では一度も成立しない** — 停滞自体は
`unresponsive_indeterminate` として escalate されるが、durable record が名指しする処方が
patient wait になり、#15842 が扱う swallowed-Enter 停滞に対して誤る。

そこで herdr delivery ledger の `notification_marker` を join する。この marker は
`[mozyo:handoff:source=…:issue=…:journal=…:kind=…:to=…]` という固定 token で、**pane content も
message body も含まない**ので hygiene 規律を破らない。

join は ledger が実際に持つものの連言とする: issue anchor / receiver が slot の role と一致 /
送信時 target が現在観測中の locator と一致 / 最新 entry。どれか欠ければ marker 無し (= join
導入前の挙動) に fail-close する。誤った marker は `unsent_composer` を主張して Enter を勧める
方向の誤りなので、fail-close の向きはこちら。

**named residual**: ledger に generation 列が無いため target 一致は同一 generation の証明では
ない。境界は `current_composer_retains_body` 自身が持つ「current composer に限る」規律であり、
古い marker が誤爆するのは「その本文が今まさに未送信で画面に出ている」場合だけ — それは class
の定義そのものである。完全に閉じるには ledger への generation 列追加が要り、本 issue の scope
外。

### 予算と冪等性

Redmine journal append は external mutation であり、`workspace_callback_supervisor` の
**pass あたり 1 external mutation** 予算を消費する。callback delivery の第一優先は反転しない。
予算が既に使われている pass では escalation は local pending に留まり、次に空いている pass で
1 件だけ書かれる。

**書き込み結果は 3 値**である。「journal id が返らなかった」は 1 つの状況ではない: 拒否された
書き込みは Redmine に届いていないが、POST が返ったのに readback できなかった書き込みは journal を
作っている**かもしれない**。前者は予算を消費せず、後者は `uncertain` として消費しなければ、同一
pass の次の workspace が未知の部分効果の後ろで 2 つ目の外部 mutation を実行する。判定には
`pass_external_budget.budget_spent()` (= `mutated OR uncertain`) をそのまま使う — sibling leg が
全部そうしているので、再導出して項を落とす余地を残さない。

deterministic no-send と見なす refusal 理由は**allowlist**である (`write_optin_unset` /
`base_url_unset` / `credential_missing` / `unauthorized` / `no_anchor` / `disabled` /
`unsupported_source`)。未知の理由・transport error・writer の raise はすべて `uncertain` に倒す。
2 種類の誤りの費用が非対称だからである: 着地した書き込みを「拒否」と呼ぶと外部 mutation が
予算から漏れるが、拒否を「不確実」と呼んでも失うのはその pass の残り 1 枠だけである。

順序は **pending → journal → readback → wake** で、各矢印に fence がある:

- pending の key は firing の identity 由来 (発火 pass の時計ではない) なので、crash 後の retry
  は衝突して 2 通目の journal を生まない。
- `mark_recorded` は空の journal id を拒否する。「多分書けた」を「書けた」と数えない。
- `mark_woken` は journal id を持たない firing を **SQL で**拒否する。存在しない journal を
  読めと coordinator を起こすのが、この rail が防ぐべき唯一の逆転だからである。
- 書き込み前にも readback を回す。既に着地した journal は束縛され、local store が何を信じて
  いようと二度と書かれない。

**非飢餓の前提は明示する。** pending は古い順に settle され、`attempts` / `last_reason` で
「拒否された書き込み」と「まだ誰も手を付けていない書き込み」を区別できる。その上で非飢餓性は
**「callback delivery はいずれ暇になる」という前提**に依存する。これは outbox drain が既に
依存している前提と同じ (恒常的に空でない outbox はそれ自体が病理) だが、前提が崩れたときに
escalation は失われるのではなく**溜まる**、そして最古 pending の age が status に出るので
silent にはならない。age を上限で縛るために delivery-first を反転することは、実装詳細ではなく
記録済み決定の変更なので、ここでは行わない。

裁定の正本: #15855 j#110121-5 / j#110121-6。


### 保存 row 全体の契約と routing integrity (j#110192 finding_1)

ここまでの 4 round は、指摘された **field を 1 つずつ**閉じてきた。その都度、次の field が開いた
ままだった。pending row については個別対応をやめ、**row 全体に 1 つの契約**を置く。

> **v0.7 の誤りの訂正**: v0.7 のこの節は「row 全体」と書いたが、実際に閉じたのは
> identity / routing / stall の列だけで、persistence state の 5 列は素通しだった。
> 適用範囲を機械照合する形への是正は下記「契約の適用範囲は『列』ではなく『全列』で定義する」
> を読む (j#110218)。

理由は「網羅的にやる方が綺麗だから」ではない。**field ごとに危険の種類が違う**からである。

- `stall_class` / `prescription` / `last_reason` は**描画される token** である。閉じた語彙で足りる。
- `lane_id` / `role` / `target` などの identity は **journal 本文へ補間**される。改行 1 つで durable
  record に行が捏造される。先頭ハイフンも同様に禁じる (後段で argv flag として読まれうる)。
- `issue` は**描画値ですらない**。**外部 write の宛先**である。そして per-field grammar は原理的に
  これを守れない — 正当な issue id と、別の正当な issue id は見分けがつかない。実測では直接 DB を
  書き換えて gate write が issue 99999 へ**転送**された。

したがって契約は 2 層になる。

1. **per-field grammar** — 閉じた語彙、長さ上限つき identity 文法、数字のみの id、正の整数の
   `consecutive` (実測値 `-3` は「変な値」ではない。`>= threshold` のどの比較にも当たらなくなる
   ので、slot を占有したまま**永久に到達不能**になる)。
2. **routing integrity seal** — `escalation_idempotency_key` の導出に **`issue` を含める**。read 時に
   row 自身の field から鍵を再導出し、保存されている鍵と照合する。不一致は「この row の routing
   facts が書かれた後に変更された」ことを意味する。per-field grammar が原理的に見られない層を、
   ここで見る。

**不一致 row を消さない**。row は「停滞が実際に発火した」という証拠であり、消すのは改竄の signal を
沈黙に変えることである。typed な verdict を stamp し、**外部効果に至る面からだけ**外す:

- `unrecorded_pending` (writer の供給面) と `unwoken_pending` (coordinator wake の供給面) は filter する。
- `open_pending` (在庫面) は **filter しない**。問題のある row を隠す在庫表は在庫表ではない。
- `quarantined_pending` と `--status` の `quarantined=` count で可視化する。この行は**非ゼロのときだけ**
  出す。常時 `quarantined=0` を出す行は、operator がやがて読まなくなる。

`telemetry()` も **field 単位**で文法を通す。all-or-nothing で伏せない: quarantine された row を見る
operator は「**どの field が**壊れているか」を知る必要があり、row ごと伏せるのは echo するのと同じ
くらい確実にそれを隠す。落ちた field は `unrenderable` token になる。

store が代替値を書く場合 (未知の refusal reason → `unclassified_reason`)、**その代替値自身が語彙の
member でなければならない**。さもなくば、守ろうとした当の row を自分で quarantine する。

### 契約の適用範囲は「列」ではなく「全列」で定義する (j#110218 finding_pendingcontract)

v0.7 は「pending row 全体に 1 つの契約を置いた」と書いた。**実際には置けていなかった。**
identity / routing / stall の列は閉じたが、persistence state の 5 列 (`journal_id` /
`written_at` / `woke_at` / `attempts` / `last_attempt_at`) は素通しだった。

**なぜ漏れたか**が、この節で残す価値のある唯一の内容である。実装者 (私) は
`idempotency_key` に**含まれる**列を契約の対象と考え、state 列を「自分が書く列だから」と
無意識に対象外にした。これは本 doc が v0.6 で自分で書いた **「store は信頼境界ではない」**
の直接の否定である。原則を書いたことと、その原則を自分の設計の適用範囲に適用することは別の
作業だった。

漏れた結果は 3 つで、深刻さの順は直感と逆だった。

1. **偽の settle** — `recorded` が `bool(journal_id)` だったので、`journal_id='not-a-journal'`
   が「書かれた」と読まれ、wake が escalation を settled にした。**存在しない journal を根拠に
   停滞報告が閉じられる**。
2. **可視化面が最初に落ちる** — 非数値の count が `int()` で `ValueError` を上げ、
   `open_pending` / `unrecorded_pending` / **`quarantined_pending`** の全読取面から漏れた。
   `--status` は「status surface must not raise」で例外を握り潰すので、**壊れた row があるほど
   画面が静かになる**。fail-closed を主張した実装が fail-silent だった。**改竄の可視化面は、
   改竄に対して最も頑健でなければならない。最初に落ちる可視化面は、可視化面ではない。**
3. **捏造された既成事実** — `attempts=-5` は「壊れた値」ではなく「試行より少ない試行回数」で
   あり、refusal の履歴を消す方向に働く。

#### 是正の形

「指摘された 5 列を足す」ではない。それは 6 round 続けた対応の 7 度目になる。

- **`PENDING_FIELD_CHECKERS`**: 全永続列 → 文法の表を 1 つ置き、**write / read / projection の
  3 面すべてが同じ表を使う**。1 つの表・両方向。field が入口だけ文法を得て出口は生のまま、
  という分裂が構造的に起きない。
- **表の完全性を test で機械照合する**。`PendingEscalation` の field 集合との**完全一致**。
  文法を持たない列を足すとそこで落ちる。次の漏れを見つけるのは reviewer ではなく test である。
- **型変換を quarantine 判定の内側へ置く**。read boundary は生値を運び、contract が typed
  verdict に倒す。**どの読取面も例外を出さない。**
- **fence は述語のある場所に置く**。`mark_woken` は key だけで到達できるので、
  canonical `journal_id` の条件は Python 側だけでなく **UPDATE の WHERE 句**にも要る。
- **quarantine の走査を lifecycle で絞らない**。`journal_id` と `woke_at` を両方書き換えた row は
  lifecycle 述語から見ると settled であり、open 行だけ走査すると**最も完全な偽造が誰にも
  見えなくなる**。

### 文法は存在を証明しない — 列の権威分類 (j#110254 finding_stateauthority / finding_checkerdrift)

v0.8 は「全永続列に文法の表を置き、write / read / projection の 3 面が共用する」と書いた。
**表は置けたが、共用は宣言だけだった。** そして表があっても閉じない層が 1 つ残っていた。

#### 誤りの形は 3 round とも同じで、抽象度だけが 1 段ずつ上がっていた

| round | 主張したこと | 実際 |
| --- | --- | --- |
| v0.7 (j#110218) | 「row 全体に契約を置いた」 | 宣言した**強制が実在しなかった** (state 5 列が素通し) |
| v0.8 (j#110254 前) | 「表を 1 つ置き 3 面が共用する」 | 強制の**適用範囲を宣言しただけ**だった |
| v0.8 の test | 「完全一致 test が次の漏れを見つける」 | **適用の証明を宣言しただけ**だった |

完全一致 test が証明するのは「表に名前が揃っていること」だけである。**各面が実際にその表を
読んでいることは 1 つも証明していない。** 実測では `journal_id` の規則について 5 つの面が
異なる答えを返していた — 表は 13 桁を拒否し、`canonical_journal_id()` (手書きの
`str.isdigit()`) と `mark_woken` の SQL 述語は受理し、`mark_recorded` は非空だけを見て
`not-a-journal` を保存し、`JournalWriteResult` も非空しか見ていなかった。原因は単純で、
**同じ規則を 2 度目に手で実装したこと**である。2 度目の実装は書いた瞬間に drift する。

#### そして文法では原理的に届かない層がある

`journal_id='999999'` は完全に canonical である。存在しないだけである。R6 で `issue` に
ついて確定した「per-field grammar は routing を守れない (99999 は正当な issue id である)」と
**同型**であり、`issue` には seal を足しながら `journal_id` には文法だけ足した。結果、偽造
された `journal_id` が Redmine への照会なしに settle し、`woke_at` も書き換えれば
**open からも quarantine からも消える**。停滞報告が「静かに完了した」ことになる。

#### 是正の形 1: 2 つの seal で全列を**分割**する

点の修正 (指摘された 2 箇所を直す) ではなく、**同型が入り込む余地そのもの**を閉じる。
round 6 は `issue`、round 7 は state 5 列、round 8 は `journal_id` を封じた。毎回「そのとき
危険が実演された列」だけを封じ、残りを次 round の指摘に残している。したがって今回封じるのは
列ではなく**分割**である。

    保存列 = IDENTITY_SEAL_FIELDS (idempotency key が封じる)
           ∪ ROW_SEAL_FIELDS      (row seal が封じる)
           ∪ {idempotency_key, row_seal}   (導出列そのもの)

`ROW_SEAL_FIELDS` は「persistence state の列」ではなく **「key が覆わない全列」** である。
`prescription` を `patient_wait_then_retry` から `owner_escalation` へ書き換える改竄は、
文法上まったく合法で、grammar には見えず、そのまま durable な Redmine journal に載る。
第 3 の分類 (「まだ誰も封じていない列」) が存在しないことを test が主張する。

#### 是正の形 2: 列を「何であるか」で分類し、分類ごとに権威を名指しする

seal は改竄を**検出**するが、「その値が正しい」ことは言わない。外部 record を名指す列には
別に権威が要る。`PENDING_FIELD_CLASSES` が全永続列を 4 分類し、各分類が保証の出所を持つ。

| 分類 | 保証する機構 | 対象列 |
| --- | --- | --- |
| `identity_component` | idempotency key の seal | `idempotency_key` / `workspace_id` / `lane_id` / `role` / `generation` / `stall_class` / `first_observed_at` |
| `external_record_reference` | key/row seal **に加えて外部システムへの照会** (`EXTERNAL_REFERENCE_AUTHORITY` が照会点を列ごとに宣言) | `issue` (write admission) / `journal_id` (wake admission) |
| `persistence_state` | row seal (`pending_row_seal`) | `written_at` / `woke_at` / `attempts` / `last_attempt_at` / `last_reason` / `row_seal` |
| `rendered_value` | row seal + 文法 + render 境界 | `target` / `prescription` / `matched_id` / `evidence_tier` / `consecutive` / `escalated_at` |

分類は**文書ではなく機械照合**である。test が (a) 分類表 == checker 表 == 保存 row の field
集合、(b) 2 つの seal が保存列を**分割**していること (交差なし・漏れなし)、(c) **全列**に
ついて「合法な別値への書き換え」が検出されること (key 側は routing mismatch、seal 側は row
mismatch)、(d) 全 `external_record_reference` が照会点を宣言し、その照会点が実際に拒否する
こと、(e) `row_seal` を除く全列が projection で文法を通ること、を列ごとに走査して確認する。
**分類だけあって機構が発火しない列は test が落とす。**

#### 各機構が何を証明し、何を証明しないか

- **row seal は改竄の証拠であって存在証明ではない。** 「store が導出した値である」ことしか
  言わない。秘密ではないので、seal ごと再計算できる攻撃者は j#110245 / j#110218 の deferred
  能力 (store への write 権限 + 全再計算) に当たる。**wake の可否をこの seal に依存させない。**
- **存在を答えられるのは外部システムだけ。** wake の admission は firing 自身の
  `idempotency_key` を持つ journal を Redmine から読み戻し、**row が主張する id と exact 一致**
  したときだけ通す (`admit_wake`)。verifier 不在・issue 読取不能・read cap 超過は
  **wake しない** (`journal_unverified`)。照会不能は「弱い根拠」ではなく「根拠なし」である。
- **照会は writer の readback と同一実装**である (`journal_id_carrying_key`)。「存在するか」を
  2 度実装すれば、それがまさに今回の drift の形になる。read は external mutation ではないが
  無料でもないので、pass 共有の `budget["reads"]` と `MAX_PROVIDER_READS_PER_PASS` で bound し、
  **上限に当たった pass は refusal として settle telemetry に現れる** (silent cap にしない)。
- **転移は洗浄しない。** 契約を満たさない row には transition を適用しない。改竄された値の上に
  新しい seal を書けば、store が次の通常 pass で偽造を `ok` に戻す共犯になる。

#### 同型の横断掃討 (この lane の他の保存列)

`journal_id` を直して終わりにすると、次 round は必ず「同型の別の列」が指摘になる。lane が
持つ 3 テーブルの全列を同じ問いで走査した結果:

- `stall_escalation_pending` — 上表のとおり分類・機械照合済み。
- `stall_watch_streak` の `generation` — 外部権威 (`LaneLifecycleStore`) の事実だが、**毎 pass
  権威から読み直して比較し、変化すれば run を破棄する** (`fold_observation` の
  generation_transition、j#110146 finding_3)。すなわち照会点は「使用の直前」で、既に
  authority-consulted-at-use である。`target` は locator であり identity ではない (evidence
  専用)。
- `stall_watch_discovery` / `stall_watch_watermark` — count と instant のみ。外部 record を
  名指す列は無い。

#### test の形を変えた

「名前が揃っている」ではなく **「同じ入力に全面が同じ答えを返す」等価性 test** にした。
`journal_id` の 26 値 corpus (12 桁 / 13 桁の境界、非 ASCII 数字、末尾改行、U+2028、前後空白)
を、checker 表 / `canonical_journal_id` / `recorded` property / `mark_recorded` /
`mark_woken` の SQL 述語 / `JournalWriteResult` / projection の 7 面に通し、**答えが割れたら
落ちる**。SQL 述語は文字列を手で書かず `canonical_numeric_id_sql()` が同じ定数から生成する。

この test は導入直後に、指摘の外側にあった 2 件を自力で検出した — `mark_recorded` の
`.strip()` (`" 110264"` を黙って直して保存する repairing face) と、`plan_recorded` が空
`journal_id` を受理する経路である。**「A を直せ」に対して A だけ直す**のではなく、A の
規則を 1 箇所にして全面を測ると、指摘されていない同型が測定で出てくる。

## Cross-References

- `vibes/docs/logics/ack-completion-receiver-state.md` — 沈黙を completion にしない正本、
  および #15842 の「起動 busy と処理 busy は event で区別できない」節
- `vibes/docs/logics/tmux-send-safety-contract.md` — 送信側 rail の挙動正本
- `vibes/docs/adr/adr-0002-enter-resend-priority.md` — Enter-only retry の owner 決定
- `vibes/docs/adr/adr-0013-ui-hides-pane-operations.md` — 手動 pane 操作を既定にしない UX 要件
- `vibes/docs/adr/adr-0014-dead-unit-proxy-recovery.md` — 事実は回収するが完了は推測しない
- `skills/mozyo-bridge-agent/references/workflow.md` — `## Wait / polling 効率標準` /
  `## Stall / no-progress 検出標準` / `## 停滞・拒否からの context reset 回復`
