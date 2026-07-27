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
| 6 | anchor | source-of-truth Redmine 上の **(action, journal, lane generation) triple**（§3） | `proxy_anchor_unverified` / `proxy_anchor_action_mismatch` / `proxy_anchor_decision_incomplete` / `proxy_anchor_generation_stale` / `proxy_anchor_superseded` |
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

- **action → 決定 token の closed map**（`ACTION_DECISION_TOKENS`）を持つ。
  `dispatch_next` → `implementation_request`。
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
  - 同一 lane についてより新しい generation が宣言されている → `generation_stale`。lane が
    advance した後の authorization は死んだ generation に属する。
  - 同一 lane・同一 generation でより新しい journal が同じ token を持つ → `superseded`。
    supersession は **その action 自身の決定系列・当該 lane の中**で判定する。無関係な後続 gate が
    正しい authorization を stale に見せてはならず、代役になってもならない。

## 3b. Acknowledgement — exactly-once の「完了」半分

`delivered` generation は route を保持し、`completed` になるまで次の decision を mint できない。
初版はこの completion を **product のどこからも呼んでいなかった**（呼んでいたのは test だけ）。
結果、最初の positive delivery の後は、より新しい durable decision すら永久に `duplicate` 拒否
される — 一度だけ動いて wedge する rail は反復可能な single-step 入口ではない（review j#89918 F1）。

- **`mozyo-bridge workflow proxy-ack --proxy-action-id <id>`** が completion の production surface。
  coordinator（または代理の operator）が、delegation が運んだ opaque id を渡す。
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

send が **発火した**ことは、着地した証拠ではない。positive delivery でない場合:

- generation は `uncertain` を保持し、blind retry しない（reconcile が先）。
- 結果は **delivery ではない**。`sent=False` / `reason=proxy_delivery_uncertain` を返し、CLI は
  **非ゼロ終了**する。caller は自前 runtime を持たず exit code で分岐するため、着かなかった委譲を
  成功として script させてはならない。rc 0 は positive delivery のときだけである。

## 6. Surface

- CLI: `mozyo-bridge workflow proxy --action <token> --source redmine --issue <id> --journal <id>`
  （既定 dry-run、`--execute` で 1 回配送）/ `mozyo-bridge workflow proxy-ack --proxy-action-id <id>`
  / `mozyo-bridge workflow proxy-fence`。
- 実装: pure matrix `...f_140_delegated_coordinator_nested_handoff/domain/coordinator_proxy.py`、
  adapter `...application/coordinator_proxy_send.py`、CLI `...application/cli_workflow_proxy.py`、
  fence `core/state/coordinator_proxy_fence.py`。
- action vocabulary は closed: `dispatch_next` のみ。決定 token を tie できる action だけを置く
  （§3）。proxy が仕事を発明することはない。

## 7. 非 goal

- caller への identity 付与、`sender_attestation` の緩和、raw pane / raw Herdr の代替提供。
- domain / design 判断、Redmine anchor の新規作成、Review Gate、owner approval、release、
  credential 操作の自動承認。
- coordinator が委譲された action を**実行する**こと自体の代行。proxy は decision を渡すだけで、
  実行は coordinator の attested runtime が自分の gate を通して行う。
