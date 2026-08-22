# Stall Watcher — 画面差分を一次センサーにした停滞検知と処方 (Redmine #15843)

## Status

- version: `v0.3` (#15855 運用配線 + j#110132 review の 4 指摘を反映。v0.1 のセンサー / 分類 / 処方の記述は不変)
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

### operator が読める runtime status

宣言した設定は `config status` が出す。**動いているかどうか**は別の面が要る。既存の
`workflow supervisor --status` に runtime readback を足した (新 surface も新 gate も作らない):

- policy の `enabled` と reason (`absent` / `declared_without_scope` / `invalid` /
  `config_unreadable` の 4 状態を区別する。malformed を absent に潰すと、cadence を打ち間違えた
  operator に「一度も設定していない」と答えることになる)、cadence と threshold の実効値;
- `last_pass_at` と `next_due_at` (後者は閾値であって予定時刻ではない);
- `out_of_reach` — live だが watcher の射程外の unit 数;
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


## Cross-References

- `vibes/docs/logics/ack-completion-receiver-state.md` — 沈黙を completion にしない正本、
  および #15842 の「起動 busy と処理 busy は event で区別できない」節
- `vibes/docs/logics/tmux-send-safety-contract.md` — 送信側 rail の挙動正本
- `vibes/docs/adr/adr-0002-enter-resend-priority.md` — Enter-only retry の owner 決定
- `vibes/docs/adr/adr-0013-ui-hides-pane-operations.md` — 手動 pane 操作を既定にしない UX 要件
- `vibes/docs/adr/adr-0014-dead-unit-proxy-recovery.md` — 事実は回収するが完了は推測しない
- `skills/mozyo-bridge-agent/references/workflow.md` — `## Wait / polling 効率標準` /
  `## Stall / no-progress 検出標準` / `## 停滞・拒否からの context reset 回復`
