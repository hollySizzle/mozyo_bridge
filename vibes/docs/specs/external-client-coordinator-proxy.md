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
| 5 | target | live inventory の **mzb1 assigned name** が decode する (workspace, provider, default lane) | `proxy_target_missing` / `proxy_target_ambiguous` / `proxy_target_locator_missing` |
| 6 | anchor | source-of-truth Redmine の structured gate marker | `proxy_anchor_unverified` / `proxy_anchor_superseded` |
| 7 | fence | dedicated exactly-once store | `proxy_duplicate` / `proxy_stale` / `proxy_fence_reconcile_required` / `proxy_fence_unavailable` |

不変条件:

- **caller env は authority ではない。** `MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` /
  `MOZYO_LANE_ID` を authority として読まない。fallback としても読まない。workspace が解決
  できない場合は `proxy_workspace_unresolved` で停止し、caller の主張へ退避しない。
- **cross-workspace は構造的に不可能。** target は agent 自身の assigned name の decode で選ぶ
  ため、foreign workspace の row は「選ばれない」のではなく「候補にならない」。
- **duplicate identity は ambiguity であって選択肢ではない。** 同一 (workspace, provider,
  default lane) に 2+ live agent がある場合、どちらへ送っても推測になるため zero-send。
- **fence は最後に評価する。** target 不正 / anchor superseded で拒否される委譲は generation を
  消費しない（修正後に同じ decision を委譲できる）。
- **順序は美観ではない。** 最初に壊れた link を報告することと、拒否が何を消費するかは同じ順序
  で決まる。

## 3. Anchor 検証（現在の決定だけを委譲する）

- 検証対象は `--issue` の journal から抽出した **structured gate marker**（machine `[mozyo:…]`
  token）のみ。散文は source にしない。
- 要求 journal が marker 集合に含まれない場合は `unverified`。**live read が失敗した場合も
  marker 集合は空**になるため、到達不能な Redmine が verified anchor に見えることはない。
- marker 集合に含まれるが最新でない場合は `superseded`。durable record が先へ進んだ決定を委譲
  することは、duplicate を委譲することと同じ欠陥である。

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

## 5. Delivery

`custom` kind の通常の anchored `handoff send` を、解決済み locator + explicit target lane /
target repo へ 1 回だけ行う。preflight・receiver binding・landing gate はすべて通常どおり通す
（proxy であることが何かを緩めることはない）。新しい handoff kind token は作らない（kind
vocabulary は closed であり、委譲は implementation request でも review request でも consultation
でもない）。summary に action / durable anchor / opaque `proxy_action_id` を載せ、coordinator 側
の記録が何を渡されたかを相関できるようにする。

## 6. Surface

- CLI: `mozyo-bridge workflow proxy --action <token> --source redmine --issue <id> --journal <id>`
  （既定 dry-run、`--execute` で 1 回配送）/ `mozyo-bridge workflow proxy-fence`。
- 実装: pure matrix `...f_140_delegated_coordinator_nested_handoff/domain/coordinator_proxy.py`、
  adapter `...application/coordinator_proxy_send.py`、CLI `...application/cli_workflow_proxy.py`、
  fence `core/state/coordinator_proxy_fence.py`。
- action vocabulary は closed: `dispatch_next` / `workflow_step`。いずれも durable record が
  既に解決し得る action であり、proxy が仕事を発明することはない。

## 7. 非 goal

- caller への identity 付与、`sender_attestation` の緩和、raw pane / raw Herdr の代替提供。
- domain / design 判断、Redmine anchor の新規作成、Review Gate、owner approval、release、
  credential 操作の自動承認。
- coordinator が委譲された action を**実行する**こと自体の代行。proxy は decision を渡すだけで、
  実行は coordinator の attested runtime が自分の gate を通して行う。
