# External-client coordinator proxy (Redmine #14546)

**external coordinator client**（attested lane agent ではない operator shell / API caller）が、
**既に durable に確定した** high-level action を live attested default coordinator へ
**一回だけ**委譲するための public rail の contract。設計正本は本 doc、role authority 側の正本は
`spec-herdr-default-lane-workflow-role-authority`。

## 1. 解く問題（observed dead end）

external client から到達できる既存 entrypoint は 2 つとも、effect の**前に**正しく停止する
（#14500 observed facts / #14546 j#89697・j#89712 で live 再現）:

- `mozyo-bridge workflow step` → `lane_unresolved / herdr_sender_identity_unresolved`
- `mozyo-bridge sublane create --execute` → pre-effect blocked（`missing_identity` +
  `sender_attestation`）、mutation は worktree 0 / branch 0 / pair 0 / dispatch 0

この停止自体は**正しい**。caller には launch-time sender identity が本当に無い。問題は
**第三の道が存在しなかった**ことで、その結果残る「前進手段」は次の 2 つだけだった:

1. `MOZYO_*` を手で export する = identity の偽造
2. coordinator の pane へ直接入力する = audit boundary の迂回

どちらも、上記 gate が守っている境界そのものを壊す。本 rail はその第三の道である。

**本 rail は gate を緩めない。** caller に identity を与えるのではなく、**caller が主張できない
もの**だけから authority を毎回導出する。委譲されるのは *decision* であって、*それを実行する
authority* ではない — 実行するのは coordinator 自身の attested runtime であり、`sublane create`
は依然 coordinator 自身の sender attestation を要求する。

## 2. Authority chain（action-time に毎回再導出する）

順序は評価順であり、報告される reason は**最初に壊れた link** である。

| # | link | 出所（caller が主張できないもの） | fail-closed reason |
| --- | --- | --- | --- |
| 1 | action | closed vocabulary との照合 | `proxy_action_unknown` |
| 2 | workspace | repo checkout の registry anchor（`herdr_workspace_segment`） | `proxy_workspace_unresolved` |
| 3 | role | repo-local durable role authority の default-lane binding | `proxy_coordinator_authority_missing` / `proxy_coordinator_authority_blocked` |
| 4 | provider | 当該 role の `provider_binding` | `proxy_provider_unresolved` |
| 5 | target | live inventory の **mzb1 assigned name** decode **＋ generation-bound startup self-attestation の join** | `proxy_target_missing` / `proxy_target_ambiguous` / `proxy_target_locator_missing` / `proxy_target_unattested` |
| 6 | anchor | Redmine 上の decision **×** lane lifecycle authority の live facts（§3） | `proxy_anchor_unverified` / `proxy_anchor_action_mismatch` / `proxy_anchor_decision_incomplete` / `proxy_anchor_lane_unresolved` / `proxy_anchor_scope_mismatch` / `proxy_anchor_generation_stale` / `proxy_anchor_superseded` |
| 7 | fence | dedicated exactly-once store | `proxy_duplicate` / `proxy_stale` / `proxy_fence_reconcile_required` / `proxy_fence_unavailable` |

不変条件:

- **caller env は authority ではない。** `MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` /
  `MOZYO_LANE_ID` を authority として読まない。fallback としても読まない。workspace が解決
  できない場合は `proxy_workspace_unresolved` で停止し、caller の主張へ退避しない。
- **cross-workspace は構造的に不可能。** target は agent 自身の assigned name の decode で選ぶ
  ため、foreign workspace の row は「選ばれない」のではなく「候補にならない」。
- **decode は necessary であって sufficient ではない。** assigned name はその slot が「何として
  launch されたか」を表すにすぎない。実際にその identity で boot し、いまその live locator を
  占有していることを attest するのは generation-bound な startup self-attestation record だけで
  ある。よって単一候補は既存 read-side policy `evaluate_attestation`（adopt classifier / doctor と
  共有。二重実装しない）で join し、record が absent / stale（別 process generation）/ conflict /
  missing なら `proxy_target_unattested` で zero-send する。**attestation store が読めない場合も
  `unattested`** であり、name 一致へ decay しない。
- **duplicate identity は ambiguity であって選択肢ではない。** 同一 (workspace, provider,
  default lane) に 2+ live agent がある場合、どちらへ送っても推測になるため zero-send。
- **fence は最後に評価する。** target 不正 / anchor superseded で拒否される委譲は generation を
  消費しない（修正後に同じ decision を委譲できる）。
- **順序は美観ではない。** 最初に壊れた link を報告することと、拒否が何を消費するかは同じ順序
  で決まる。

## 3. Anchor 検証 — authority の単位は **(action, journal, lane generation)**

action を closed vocabulary で検証し、journal を marker 集合で検証しても、**両者を突き合わせない
限り「決定」を検証したことにはならない**。初版はこの join を欠いており、任意の in-vocabulary
action が任意の gate journal に乗れた（`implementation_done` が `dispatch_next` を authorize でき
た）。さらに token と journal が一致しても、その決定が **どの lane のどの generation** を
authorize したかを照合しなければ scope は未検証のままである（review j#89918 F2）。authority の
単位はこの三つ組であり、どれか一つずつではない。

- **action → 決定 token の closed map**（`ACTION_DECISION_TOKENS`）を持つ。両 action とも
  `implementation_request`。
- **action → scope の closed map**（`ACTION_SCOPES`）を持つ。同じ決定 token でも、**どの live fact
  と突き合わせるか**が action ごとに異なる（review j#90068 F1）:
  - `dispatch_next` = `lane_scoped`。決定は lane と数値 generation を名乗り、その lane の live
    lifecycle facts と exact-match する。
  - `bootstrap_lane` = `issue_scoped`。**lane がまだ存在しない状態**で成立する必要があるため、決定は
    lane を名乗ってはならず（名乗る決定は lane-scoped の誤用 = `scope_mismatch`）、突き合わせ対象は
    「その issue が active lane を**所有していない**こと」である。所有していれば precondition は既に
    過ぎており `scope_mismatch`（呼ぶべきは `dispatch_next`）。
  - **全 action を lane に突き合わせる契約は、本 rail の起点そのものに到達できない。** 観測された
    dead end は `sublane create --execute` が pre-effect 停止し lane / worktree / pair が 0 の状態で
    あり、lane を前提にする rail は「lane を作れない」という元の defect を解かない。
- 決定 token を **tie できない action は語彙に置かない。** `workflow_step` はこの理由で撤回した:
  「一段進める」を authorize するのは「その時点で next action を名指す gate」であって固定 token
  ではなく、mapping を発明すれば同じ未検証 join を別の形で持ち込むだけになる。委譲可能面を狭める
  のが fail-closed な答えであり、具体的な決定 token が特定できた時点で広げればよい。
- marker の読み取りは **generic workflow-event token**（`gate` / `kind`）で行う。callback 用
  `GATE_BEARING_KINDS` は「coordinator を起こすべき状態」だけを覆い、`implementation_request` は
  意図的に**除外**されている。callback reader 経由で読むと、`dispatch_next` を authorize する唯一の
  決定が見えない一方、無関係な gate は anchor として使えてしまう。
- journal id は marker を持つ **entry 自身の `journal_id`** を使う。marker の自己申告 `journal=`
  は使わない（note が自分の anchor を名乗ってはならない）。散文は source にしない。
- canonical dispatch marker が持つ `lane` / `lane_generation` を**捨てずに保持**し、照合に使う
  （review j#89918 F2）。これらを落とすと、token と journal が一致しただけで authorization が
  成立してしまう。
- 分類:
  - 当該 issue の workflow-event marker をどれも持たない journal → `unverified`。**live read 失敗も
    marker 集合が空**になるため、到達不能な Redmine が verified anchor に見えることはない。
  - marker は持つが当該 action の token ではない → `action_mismatch`。anchor は実在し、**対**が
    成立していない。どちらが誤りかを operator に区別可能な形で返すため `unverified` と分ける。
  - 当該 action の token を持つが `lane` / 数値 `lane_generation` を欠く → `decision_incomplete`。
    canonical producer は常に両方を書く。**scope を名指さない決定は何も authorize しない** —
    live fact と exact-match できないからである。散文中の marker-shaped **引用**もここへ落ちる
    (引用された gate token は lane も generation も持たない)。
  - 名指された lane の **live lifecycle facts が読めない** → `lane_unresolved`。runtime が知らない
    lane を名乗る決定は、この rail が行動できる決定ではない。
  - 決定の lane が解決された lane と異なる → `scope_mismatch`。
  - 宣言 generation が lane の **live generation** と異なる → `generation_stale`。
  - journal が lane lifecycle の **current decision anchor** でない → `superseded`。これが
    **canonical-shaped marker の散文引用**を落とす箇所である: 引用は token / lane / generation を
    完全に再現できるが、lifecycle が指している journal ではない。

**決定を決定自身と突き合わせても何も証明しない。** 初版はこの自己比較だった（marker 集合内で
lane/generation を比べるだけ）。その結果、単独 marker は「非空 lane + 数値 generation」を名乗る
だけで verified になり、(a) 実在しない lane/generation を名乗る決定、(b) 実 lane が advance した
のに新 marker が書かれていない古い決定、(c) canonical-shaped な引用、のいずれも通過した
（review j#89969 F2）。expected facts は **action-time に lane lifecycle authority**
(`LaneLifecycleStore`、worker-dispatch admission が join するのと同一の authority) から解決し、
caller の主張にも marker の自己申告にも依存しない。active でない / generation 0 / 行が無い lane は
`None` を返し fail-closed。

## 3b. Acknowledgement — exactly-once の「完了」半分

`delivered` generation は route を保持し、`completed` になるまで次の decision を mint できない。
初版はこの completion を **product のどこからも呼んでいなかった**（呼んでいたのは test だけ）。
結果、最初の positive delivery の後は、より新しい durable decision すら永久に `duplicate` 拒否
される — 一度だけ動いて wedge する rail は反復可能な single-step 入口ではない（review j#89918 F1）。

- **`mozyo-bridge workflow proxy-ack --proxy-action-id <id>`** が completion の production surface。
- **action id の所持は credential ではない。** delegation の envelope はその id を **external
  client 自身**へ返す。所持だけを authority にすると、caller は配送直後に自分の delegation を
  complete して次の decision の配送を開けられる — delivery receipt を completion truth へ昇格させる
  ことであり、`logic-ack-completion-receiver-state` が明示的に分離している誤りである
  （review j#89969 F1）。
- したがって ack は **live attested default coordinator 自身からのみ** 受理する。他の authority
  link と同じく action-time に再導出する: attested launch-time sender identity の存在 / workspace が
  この checkout の registry anchor と一致 / default lane に座る / provider が `provider_binding` の
  期待値と一致 / **その slot 自身が generation-bound startup self-attestation join を通り、live
  default-lane slot の assigned name と一致する**。external client は identity を持たないため
  `proxy_ack_unattested` で拒否される。
- authority check は **store に触れる前**に行う。admission されない caller は fence へ到達しない。
- operator による代行はこの surface では admit しない。代行を許すなら独自の durable authority
  anchor が要るが、本 contract の scope 外である。
- ack を **暗黙の副作用にしない**。主張している内容が「coordinator が委譲された decision を実行
  した」であり、delivery path から観測できる事実ではないため。outcome 不明の delegation は route
  を保持し続けるのが正しい。
- CAS は **positive delivery のみ**から進む。unknown / stale id、`reserved` / `uncertain`
  generation、別 workspace はいずれも no-op + 非ゼロ終了。ack は「着かなかったもの」を塗り潰さない。
- ack しても **同一 decision は再開しない**（§4）。supersede する decision だけが次の generation を
  mint する。

## 4. Exactly-once fence（`core/state/coordinator_proxy_fence.py`）

route key = `(workspace_id, lane_id, role, action)`。target の live assigned name は action-time
attestation であり key に含めない（target rename が generation を進めない）。row は委譲した
**durable decision**（`issue` + `journal`）を保持する。これが sibling fence との決定的な差で、
理由は caller の retry 形態にある — caller には runtime が無いので「もう一度 command を叩く」が
通常の retry である。

- **completed は同一 decision に対して route を再開しない。** 他の fence は completion 後に
  re-mint する（繰り返す forward ではそれが正しい）が、本 fence はしない。再開すれば無害な
  再実行が「coordinator が既に実行した action の二度目の配送」になる。
- **supersede しない journal は stale。** Redmine journal id は整数なので比較は**数値**で行う
  （文字列比較では `"9" > "10"` となり古い決定が新しく見える）。非数値 journal は fail-closed。
- crash window（未解決の reserve）は `uncertain` へ遷移し、blind retry しない。
- store identity は DB-external `store_nonce` sidecar。**execution path は store を
  auto-bootstrap しない** — 損失後の silent re-create は、既に delivered な委譲の再送を許す。
  init / recovery は `workflow proxy-fence --bootstrap` / `--recover` のみ。

## 5. Delivery — 「送った」と「着いた」を混同しない

`custom` kind の通常の anchored `handoff send` を、解決済み locator + explicit target lane /
target repo へ 1 回だけ行う。preflight・receiver binding・landing gate はすべて通常どおり通す
（proxy であることが何かを緩めることはない）。新しい handoff kind token は作らない（kind
vocabulary は closed であり、委譲は implementation request でも review request でも consultation
でもない）。summary に action / durable anchor / opaque `proxy_action_id` を載せ、coordinator 側
の記録が何を渡されたかを相関できるようにする。

**outcome write の CAS 結果は必ず観測する。** `mark_delivered` / `mark_uncertain` は generation が
まだ `reserved` の場合にだけ成功する CAS である。競合 retry が送信中に reserve へ再入すると row は
`uncertain` へ遷移し、その後 positive delivery が戻っても CAS は False になる。この結果を無視すると
caller には rc 0 を返しながら store は `uncertain`、`proxy-ack` は delivered-only なので route は
完了不能に wedge する（review j#90032 F2）。**store が記録しなかった delivery は delivery ではない。**
CAS False は success にせず `proxy_delivery_uncertain` + 非ゼロ終了へ落とし、blind retry しない。

send が **発火した**ことは、着地した証拠ではない。positive delivery でない場合:

- generation は `uncertain` を保持し、blind retry しない（reconcile が先）。
- 結果は **delivery ではない**。`sent=False` / `reason=proxy_delivery_uncertain` を返し、CLI は
  **非ゼロ終了**する。caller は自前 runtime を持たず exit code で分岐するため、着かなかった委譲を
  成功として script させてはならない。rc 0 は positive delivery のときだけである。

## 5b. Executable leg wiring

resolver が `execution=ready` + direction 別 primitive を返しても、**CLI の executable-leg
classifier がその primitive を認めなければ何も発火しない**。初版は classifier が既存 2 token を
手書き列挙していたため、`herdr_forward_managed_gateway` は解決可能かつ発火不能で、`workflow step`
は rc 0 / `execution: ready` を返して何も送らなかった。本US の中核 acceptance がコード上到達不能
だった（review j#90032 F1）。

- classifier の membership は **route matrix から導出**する（`FORWARD_PRIMITIVES`）。手書き列挙は
  しない。direction を追加して executor を置き去りにすることが構造的に起こらないようにする。
- forward leg は専用 fence と専用 executor に乗るため、generic `WorkflowStepOutcome.executable`
  集合には**入れない**（そちらは tmux primitive rail）。両者の関係は coherence test で固定する。
- 非 dry-run で leg 1 回 / dry-run で 0 回 / leg の rc 伝播を **top-level CLI** で回帰化する。

## 6. Surface

- CLI: `mozyo-bridge workflow proxy --action <token> --source redmine --issue <id> --journal <id>`
  （既定 dry-run、`--execute` で 1 回配送）/ `mozyo-bridge workflow proxy-ack --proxy-action-id <id>`
  / `mozyo-bridge workflow proxy-fence`。
- 実装: pure matrix `...f_140_delegated_coordinator_nested_handoff/domain/coordinator_proxy.py`、
  adapter `...application/coordinator_proxy_send.py`、CLI `...application/cli_workflow_proxy.py`、
  fence `core/state/coordinator_proxy_fence.py`。
- action vocabulary は closed: `bootstrap_lane` / `dispatch_next`。決定 token を tie できる action
  だけを置く（§3）。proxy が仕事を発明することはない。

## 7. 非 goal

- caller への identity 付与、`sender_attestation` の緩和、raw pane / raw Herdr の代替提供。
- domain / design 判断、Redmine anchor の新規作成、Review Gate、owner approval、release、
  credential 操作の自動承認。
- coordinator が委譲された action を**実行する**こと自体の代行。proxy は decision を渡すだけで、
  実行は coordinator の attested runtime が自分の gate を通して行う。
