# Session continuity user harness 仕様

Redmine #13529「新規session移行packageでactive workflowと承認境界をdurableに復元できる」。新規 session が chat memory や pane scrollback に依存せず、現在地を復元するための user harness の構成と session 移行 package を定義する。

## Harness の構成

| layer | 正本 / 役割 | 時点依存 state |
| --- | --- | --- |
| router | `AGENTS.md` / `CLAUDE.md`。tool-specific 入口 | 持たない |
| governance | central preset と `.mozyo-bridge/config.yaml` | role/provider の現運用値だけ config で保持 |
| knowledge | cataloged `vibes/docs/` | 恒久的な rule / logic / spec / task だけ |
| durable work state | Redmine issue / journal | active gate、承認、検証、次 actor |
| transition bundle | `vibes/docs/temps/session-handoff-<issue>.md` | 次 session が消費するまでの pointer 束 |
| formatter | `mozyo-bridge session boundary-prompt` | latest journal を指す pasteable prompt |

優先順位は router が示す命令順位に従う。通知本文と transition bundle はいずれも pointer であり、Redmine source journal を上書きしない。通知の kind / summary と journal が矛盾する場合は journal を採用し、判断を fail-closed にする。

## 文書配置

- `vibes/docs/logics/`: 業務ロジック。
- `vibes/docs/rules/`: プロジェクト規約。
- `vibes/docs/specs/`: 仕様。
- `vibes/docs/tasks/`: 再実行可能な手順書。
- `vibes/docs/temps/`: 一時ドキュメント。恒久参照にせず、消費後に削除する。

進捗 snapshot、lane の live 状態、時限的な承認範囲を rules / logics / specs / tasks に焼かない。恒久文書は discovery と contract に限定し、現在値は Redmine と transition bundle に置く。

## Session 移行 package

境界では次を一組として用意する。

1. active な session-transition US と、その Task / Test / 実在する Bug。
2. Redmine の最新 journal。active chain、承認範囲、preservation signal、次 actor を記録する。
3. `vibes/docs/temps/` の transition bundle。複数 issue の subject・role・latest journal をまとめるが、authority にはしない。lane を記す場合は Git branch/worktree、registered lane metadata、live routable runtime を別 state として区別する (`### Routable lane state の区別`)。
4. `logic-session-boundary` に従う next-session prompt。先頭で source-of-truth journal の再読を命じる。

bundle には host-local absolute path、credential、secret、pane scrollback を含めない。repo は portable identifier、execution root は repo-relative pointer で表す。

### Repository identity の曖昧性排除

portable identifier は pane 名や現在の cwd ではなく workspace registry から解決する。canonical session 名だけで複数 record が一致する場合は、bundle / journal に記録された `workspace_id` を併用し、候補 checkout の存在、Redmine project、Git origin が対象 repo と一致することを検証する。一意に決まらなければ推測で選ばず fail-closed にする。host-local absolute path は引き続き bundle / journalへ記録しない。

### Release-readiness evidence

release chain を引き継ぐ bundle は、local verification だけで「release可能」と判定しない。少なくとも次の既知状態を分けて記録する。

- active review の latest journal、review finding、対象 commit と origin 到達性。
- `origin/main`、integration lane、active issue lane の既知 head。
- latest main CI run の識別子と conclusion。原因未分類の失敗は未解決リスクとする。
- package version decision、version bump、build、artifact inspection、TestPyPI publish、TestPyPI exact-version install、installed CLI E2E の各 gate。
- owner 承認の対象と除外。TestPyPI 承認を production PyPI / GitHub Releaseへ拡張解釈しない。

### Installed environment evidence

公開候補・公開済み artifact を source checkout の外で検証する session を引き継ぐ場合、package の正しさと実行環境の正しさを collapse しない。transition bundle / journal は少なくとも次を独立して記録する。

```yaml
installed_environment_evidence:
  artifact:
    source: workflow_artifact | testpypi | pypi
    exact_version: <version>
    source_sha: <origin-reachable commit>
    local_substitution: false
  environment:
    image_ref: <public image tag or digest>
    reproducibility: pinned_digest | floating_canary
    runtime_user: non_root | root
    home_state: fresh | persistent
    source_checkout_mount: absent | present
  verification:
    surfaces: []
    result: passed | failed | blocked | not_run
    external_live_scope: excluded | separately_verified
```

- blocking gate は再現可能な pinned image digest と exact artifact authority を使う。floating image tag は OS drift canary として別 verdict にし、blocking gate の証拠へ読み替えない。
- published index install を要求する acceptance で local wheel / editable install / source mount を代替証拠にしない。workflow artifact の pre-publish smoke は別の artifact source として明示する。
- container 内の command smoke は real agent TUI、tmux / terminal transport、外部 ticket service の live E2E を自動的に保証しない。実施していない面は `external_live_scope: excluded` として残す。
- image、repository、journalへ credentialを格納しない。外部 serviceを検証する場合はsecret injectionとその値を記録せず、別gateで結果だけを残す。
- failureを観測しただけで既知 product defectと断定しない。exact artifact / image / command / expected / actualを再現し、Bugへ切り出した durable anchorを記録する。

### Routable lane state の区別

lane を bundle / journal に記す場合、「target lane label」を live で routable な herdr lane と同一視しない。次の 3 state を **独立して** 記録し、collapse しない (Redmine #13543)。

```yaml
lane_state:
  git_branch_worktree:      # Git branch head / worktree の存在。`git branch` / worktree で確認
    - present | absent
  registered_lane_metadata: # lane_metadata record の存在。live inventory row の存在とは別に判定
    - present | absent | unknown
  live_routable_runtime:    # dispatch 可能な live herdr slot (gateway/worker pane) の存在
    - present | absent
```

3 state から routable verdict を導く。routable でないときは **欠落した layer を名指しで fail-closed** にし、次 action の可否根拠にする。

```yaml
routable_verdict:
  routable:            # 3 state すべて present。標準 dispatch の対象
  branch-only:         # git_branch_worktree=present だが他 2 state が absent
  lane-unregistered:   # registered_lane_metadata=absent。branch / live runtime の値は別途併記
  runtime-unavailable: # registered_lane_metadata=present だが live_routable_runtime=absent
```

- `sublane list --lane <label> --json` の `sublanes` 非空を、そのまま metadata record の存在証明にしない。backend=herdr の projection は live assigned-name row だけでも lane row を返す。返却 row の `stale_hints` に `lane_record_missing` があれば `registered_lane_metadata=absent`、`lane_slots_missing` があれば metadata は present だが live runtime は absent と判定する。`sublanes: []` は対象 lane の record / live row がどちらも projection に現れなかったことを示す。store / projection が unreadable、または record-backed か判別できない出力は `unknown` として fail-closed にする。
- primary verdict は次の順に判定する: 3 state presentなら `routable`; metadata absent + live runtime presentなら `lane-unregistered`; Git present + metadata absent + live runtime absentなら `branch-only`; それ以外のmetadata absentは`lane-unregistered`; metadata present + live runtime absentなら `runtime-unavailable`。metadata unknownではverdictもunknownとしてfail-closedにする。`lane_record_missing` は metadata layer 欠落の証拠に限り、live routing / liveness の存在・不在を導かない。live locator を観測しても metadata を推測で再構成せず、issue / lane attribution の durable dispositionを先に作る。
- routable でない lane を「dispatch 先が消えた / 送達失敗」と report しない。正しい理由は上記 verdict のどれか (branch は在るが lane 未登録、等) であり、bundle の `target lane` 記載だけを根拠に routable と判定しない。
- backend=herdr では `agents targets` は tmux-era primitive/debug 面 (`### Runtime fingerprint gate (backend=herdr)` / `task-herdr-lane-operations`)。その empty candidate を「routable lane 不在」の根拠にしない。lane state は `sublane list --lane <label> --json` の row / `stale_hints` と live slot 実測を合わせて判定する。

### Runtime fingerprint gate (backend=herdr)

backend=herdr の repo では、lane discovery / dispatch / handoff 可否の next-action を確定する前に、実行中 CLI の runtime fingerprint を照合する (Redmine #13543、正本 helper: `mozyo-bridge doctor runtime` = #12612)。

- **standard surface と debug/primitive surface を区別する**。標準 lane 面は `sublane create` / `sublane list` / `sublane dispatch-worker` (herdr-native)。`agents targets` / `handoff send --select` / `message --select-role` / 明示 `%pane` は tmux-era の primitive/debug 面であり、その出力を dispatch / handoff-blocker の authority にしない (`task-herdr-lane-operations` `## 標準入口 vs primitive/debug 面`)。
- **installed / source fingerprint mismatch を next-action 前に fail-closed で記録する**。`mozyo-bridge doctor runtime` を read-only 実行し、installed CLI が source checkout の herdr preflight 等の gate-critical behavior を欠く場合 (`status: drifted` = same-version-probe-drift、または probe mismatch) は、その skew を durable record に明記し、installed CLI surface の出力を next-action の根拠にしない。
- fingerprint mismatch の間は repo-local source CLI (`PYTHONPATH=src python3 -m mozyo_bridge <args>`) を lane discovery / dispatch に使う。installed CLI upgrade / reinstall は owner-gated であり、fingerprint gate はそれを要求せず fallback 経路を指す。

### Sender command-shell / auth state

live inventory 上のagentと、handoff commandを実行するprocessは同一stateとは限らない。session transitionでは次を独立して記録する。

```yaml
runtime_execution_state:
  agent_auth_state: authenticated | authentication_required | dead | unknown
  sender_identity_state: present | missing | conflict | not_measured
  delivery_state: not_attempted | sent | blocked
```

- `agent_auth_state` はpaneのread-only実測で分類する。credentialや認証内容そのものは記録しない。logout / `authentication required` / agent process終了はrouting failureに分類せず、re-authまたはfresh agent relaunchの対象にする。
- `sender_identity_state` は **handoffを実行する同じcommand context** で `MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` / `MOZYO_LANE_ID` とrepo anchorの整合を確認する。assigned nameやTUI launch envが見えることだけでは`present`にしない (Redmine #13614)。
- `missing` / `conflict` ではstandard handoffをfail-closedにする。手動env注入、raw `herdr agent send`、cross-lane Claude direct sendを復旧経路にしない。
- `sublane create --execute`がagentを起動した後にsender attestationで失敗した場合、launch済み/未配送のpartial stateを明示し、resume/retireをdurableにする。成功扱いにも無言の再実行にも進めない (Redmine #13613)。

## 更新契機

次のいずれかで package を更新する。

- active issue / parent / Version / repo / execution root が変わった。
- Implementation Done / Review / QA / Close など gate が遷移した。
- owner の承認範囲が変わった。
- 新規 session へ移る、または context pressure が高まった。

worker の進行中に snapshot を追記し続けるための polling は行わない。境界作成時に source journal を一度再読し、既知の最新 anchor を記録して turn を終了する。後続 callback があれば、新規 session は必ず Redmine の latest journal を再取得する。

## Acceptance

fresh session は prompt だけを入口として、次を復元できなければならない。

- ticket IDだけでなく subject、issue role、parent、latest journal。
- active work と dependency 順の queued work。
- owner 承認の対象と明示的な除外。
- commit / branch / verification の既知状態。
- lane を扱う場合、Git branch/worktree・registered lane metadata・live routable runtime を区別した routable verdict と、backend=herdr の runtime fingerprint (installed/source skew) を next-action 前に確認できる。
- agent auth state、実command shellのsender identity state、delivery stateを独立して復元できる。
- preservation signal と、実行してはいけない操作。
- pending action と next actor。

復元後は transition bundle を authority に昇格させず、source journal と cataloged docs で照合する。受領を durable journal に記録した後、stale bundle は削除対象にする。

stale bundle の削除は、次のtransition bundleを作るreviewable commitに含めるか、受領journalへ削除延期と理由を明記する。受領済みbundleをlive pointerに見える状態で黙って残さない。
