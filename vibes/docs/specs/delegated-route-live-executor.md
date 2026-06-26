# delegated route live executor contract

Redmine #12556。#12550 の pure planner / operator-confirmed command plan を、
real-machine smoke 前に live cockpit / tmux / Redmine side effects へ接続する executor
contract。

本書は「planner が正しい command plan を出す」段階と「実機状態を mutate して Redmine に
replayable evidence を残す」段階を分ける。#12546 autonomous parent real-machine smoke は、
本 executor が実装・review 済みになってから実施する。

## 目的

live executor が担うのは、planner output をもとに次の side effect を安全に実行し、結果を
durable record に残すことである。

- cockpit lane / window の append または adopt。
- managed lane / pane の role profile / projection metadata stamping。
- child Codex gateway / grandchild gateway / same-lane worker への `handoff send`。
- baseline / parent decision / child delivery / grandchild realization / worker evidence /
  callback outcome / final classification の Redmine record package。

executor は acceptance oracle ではない。PASS / failed_acceptance / insufficient /
contaminated / blocked / environmental の分類語彙は
`delegated-coordinator-real-machine-acceptance.md` と
`delegated-coordinator-smoke-test-frame.md` を正本とする。

## 入力境界

executor の入力は planner output と route identity record である。

- planner output: #12550 `delegation_route_planner` の typed route plan。
- route identity: #12553 `route-identity-ledger.md` の stable route identity。
- Redmine anchor: 実行対象 issue / journal id。
- runtime providers: tmux / cockpit / Redmine / handoff sender の provider interface。

executor は parent config resolver の生 config を再解釈しない。child candidate 解決は
#12549 の resolver、route plan 生成は #12550 の planner が担う。

## Side Effect 境界

executor は side effect を provider 経由に閉じ込める。

- domain layer は command sequence と expected durable records を構築する。
- application layer は provider を呼び出し、observed outcome を record package に変換する。
- tests は fake tmux / fake Redmine / fake handoff provider を使い、実 tmux や Redmine を
  mutate しない。
- real-machine smoke は #12546 の scope であり、本書の unit / integration tests では実行しない。

## Route Identity と Pane ID

`pane_id` は current send target の cache / runtime evidence に限る。executor は送信直前に
stable route identity を live inventory へ再照合する。

必須:

- `workspace_id`, `lane_id`, `role`, `pane_name`, `callback_purpose` を使って target を
  再解決する。
- 候補 0 件は `target_unavailable`。
- 候補複数または ambiguous row は `target_ambiguous`。
- identity mismatch は送信前に blocked。
- resolved `pane_id` を Redmine に残す場合は runtime evidence と明記し、route authority
  として扱わない。

## Handoff 安全境界

- cross-project / cross-lane Claude direct send は生成・実行しない。
- lane boundary を跨ぐ場合は target lane の Codex gateway へ送る。
- same-lane Claude dispatch は target repo identity gate と submit-complete rail を通す。
- `marker_timeout` や target mismatch は `environmental` または `blocked` evidence として
  記録し、PASS evidence に混ぜない。
- low-level `type` / `keys` / manual Enter recovery を使った場合は recovery として記録し、
  standard delivery と同一視しない。

## Redmine Record Package

executor は少なくとも次の journal package を作れること。

```yaml
record_package:
  baseline:
    - fresh pane/worktree/config snapshot
    - stale evidence exclusion note
  parent_decision:
    - selected child project
    - config/context basis
    - forbidden hints absent
  child_delivery:
    - target Codex gateway identity
    - target repo gate result
    - handoff command/outcome
  child_result:
    - child issue or implementation request
    - grandchild dispatch decision
  grandchild_realization:
    - lane/window creation_or_adoption
    - stamp/projection result
  worker_evidence:
    - worker fresh projection
    - product result separated from route evidence
  callback_outcome:
    - grandchild -> child -> parent callbacks
  final_classification:
    - PASS | failed_acceptance | insufficient | contaminated | blocked | environmental
    - reason
```

Redmine record は pane notification の代替ではなく source of truth である。executor が
notification に成功しても、対応する journal がない場合は acceptance evidence としない。

## Fail-Closed 条件

次は実行を止める、または non-PASS classification として記録する。

- planner disposition が `read_only_recommendation` / `blocked` / `failed_acceptance`。
- required child lane/window を append/adopt できない。
- grandchild required なのに same-lane worker へ fallback する。
- route identity が missing / ambiguous / stale mismatch。
- target repo identity gate が失敗する。
- direct cross-project Claude send になり得る route。
- Redmine journal write が失敗し replayable record を残せない。
- private pane id / host path / cockpit composition を tracked fixture に固定する必要が生じる。

## Test Expectations

実装時は少なくとも次を fake provider tests で固定する。

- planner の `operator_confirmed_command_plan` から cockpit append/adopt step が順序通り実行される。
- append/adopt failure は `blocked` または `environmental` になり、PASS にならない。
- stamp 後の projection が `KIND` / `DEPTH` / `PARENT` を満たさない場合は fail-closed。
- route identity re-resolution が target unavailable / ambiguous / mismatch を区別する。
- Codex gateway route が使われ、foreign Claude direct send が provider 層でも拒否される。
- Redmine record package が baseline から final classification まで欠落なく構築される。
- provider failure 後の retry は duplicate journal / duplicate send を起こさない。

## 実装対象候補

予定 path:

- `src/mozyo_bridge/domain/delegation_route_executor.py`
- `src/mozyo_bridge/application/delegation_route_executor.py`
- `tests/test_delegation_route_executor.py`
- `tests/test_delegation_route_executor_integration.py`

具体 path は実装時に調整してよいが、catalog resolver で本書が引けることを維持する。

## 参照正本

- `vibes/docs/logics/delegated-coordinator-real-machine-acceptance.md`
- `vibes/docs/logics/delegated-coordinator-smoke-test-frame.md`
- `vibes/docs/specs/delegation-policy-project-config.md`
- `vibes/docs/specs/delegated-coordinator-decision-records.md`
- `vibes/docs/specs/delegated-coordinator-role-profile.md`
- `vibes/docs/specs/route-identity-ledger.md`
- `vibes/docs/logics/tmux-send-safety-contract.md`
- `skills/mozyo-bridge-agent/references/workflow.md`

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve src/mozyo_bridge/domain/delegation_route_executor.py --repo . --format text`
