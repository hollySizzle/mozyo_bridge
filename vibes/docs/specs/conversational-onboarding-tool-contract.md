# 対話型 onboarding tool contract

Redmine #13424。未採用 directory で人間が bare `mozyo` を一度実行した後、CLI flag や
YAML を覚えず、会話から安全な project adoption と herdr 起動まで到達するための設計正本。
MVP は非エンジニアの単一 project 初期設定に限定し、汎用 scaffolding assistant は作らない。

## 目的と非目標

- LLM は自然言語を closed schema の tool call へ変換する UI だけを担う。
- filesystem、config、scaffold、workspace、runtime の mutation は決定論 tool が担う。
- home、同期 folder、Git、backend、preset、既存 state を model 起動前に機械判定する。
- 既存の `scaffold apply`、`rules install`、`workspace register`、`mozyo --json` を再利用する。
- owner approval、Redmine project 選択、credential、release、destructive cleanup は自動化しない。
- model に shell、任意 file write、任意 network、YAML 生成 tool を渡さない。

## Actor authority

| actor | owns | does not own |
| --- | --- | --- |
| Human | 目的の説明、caution の確認、mutation plan の最終確認 | CLI flag / YAML authoring |
| Preflight | path / adoption / Git / sync / binary の機械判定 | user intent、preset 推測 |
| Conversation | 自然言語から `OnboardingIntent` への変換 | mutation、gate override、approval |
| Orchestrator | state machine、schema validation、plan / apply / resume | domain decision の捏造 |
| Deterministic tools | scaffold / config / rules / registry / launch の実行 | 会話、owner approval |

source of truth は target root の実 file と typed config、workspace registry、tool outcome である。
会話 transcript は workflow truth ではなく、repo や ticket に保存しない。

## Model 起動前 hard gate

```yaml
onboarding_preflight:
  input: canonical cwd
  output:
    state: adopted | unadopted | adoption_in_progress | blocked | caution_requires_ack
    root_kind: git | non_git
    path_risk: normal | home | sync_or_cloud | ambiguous
    adoption_marker: absent | config | scaffold | workspace_anchor | onboarding_receipt
    herdr_binary: {state: resolved | missing | ambiguous, source: env | path | none}
  hard_block:
    - canonical root が home
    - cwd / symlink / mount identity が一意に解決できない
    - unreadable existing config または壊れた onboarding receipt
  caution_requires_human_ack:
    - sync_or_cloud
  invariant:
    - sync_or_cloud では git_mode=initialize を常に拒否する
    - model は block / caution を解除できない
```

sync/cloud 判定は既知 provider 名だけでなく、canonical path の platform-specific sync root と
mount metadata を deterministic classifier が返す。判定不能を `normal` に倒さず `ambiguous`
で止める。MVP の同期 folder は non-Git のまま採用する。

caution の確認は model を起動する前に CLI / orchestrator が Human から直接取得し、root
fingerprint と risk を束ねた opaque `human_gate_receipt_id` として保持する。model が確認値や
receipt 本文を生成・変更する経路は持たない。receipt が無い / root と一致しない場合は plan を作らない。

herdr binary は repo-local config から読まない。解決順は trusted environment の
`MOZYO_HERDR_BINARY`、次に trusted `PATH` 上の executable `herdr` とし、realpath と executable
bit を検証する。PATH 解決値は launch agent へ絶対 path で注入する。

## Closed conversation schema

```yaml
OnboardingIntent:
  schema_version: 1
  action: explain | propose | confirm_plan | revise | cancel
  preset: none | asana | redmine | redmine_governed | redmine_rails | redmine_rails_governed | undecided
  backend: herdr
  git_mode: existing | none | initialize
  rules_store: central | repo_local
  free_text_summary: string
```

`free_text_summary` は表示専用で mutation input にしない。unknown enum / key、欠落 field、model の
shell command、file content、credential-shaped value は reject し、conversation へ構造化 error を
返す。`undecided` は追加質問を許すが plan を作れない。`git_mode=initialize` は通常 path でも
Human の独立確認を要求し、MVP acceptance では使用しない。

MVP の対象は fresh non-Git sync folder + `preset=none` + `backend=herdr` + central rules store
に pin する。会話は目的説明とplan確認には使うが、この acceptance でpresetを推測させない。
Redmine / Asana を明示的に選ぶ拡張は別caseとし、Redmine default projectが未確定ならplanを
作らずoperator decisionへ返す。

## Deterministic tool surface

```yaml
tools:
  onboarding.inspect:
    mutation: none
    result: OnboardingPreflight
  onboarding.plan:
    input: {intent: OnboardingIntent, human_gate_receipt_id: opaque | none}
    mutation: none
    result: {plan_id, root_fingerprint, ordered_steps, warnings, requires_confirmation}
  onboarding.apply:
    # apply が plan record を運ぶ場合、closed schema / unknown-key を reject し、
    # human-visible field 全て (canonical root / fingerprint / intent / gate receipt /
    # preset / store / exact ordered steps + summaries / warnings / requires_confirmation /
    # plan_id HMAC) を fresh 再構築 canonical plan と厳密一致させる。一致しなければ plan_unauthorized。
    input: {plan_record (plan_id + closed intent + gate receipt + display fields), human_confirmed: true}
    mutation: bounded
    result: {state, applied_steps, no_op_steps, failed_step, next_action}
  onboarding.resume:
    input: current root
    mutation: one pending idempotent step
    result: same as onboarding.apply
```

`onboarding.plan` は `onboarding.inspect` を決定論的に再実行してpreflight factsを得る。model
由来の path / risk / adoption / binary fact は入力として受け付けない。`plan_id` は trusted secret
鍵の HMAC authority token であり、canonical root、root fingerprint、再取得したpreflight facts
(state 含む)、intent の全 closed field、existing file hashes、binary realpath、必要なhuman gate
receipt、exact ordered steps / preset / store を束縛する。caller が再計算できる非鍵 hash は
authority にしない。`apply` は fresh 再 inspect した facts と closed intent から canonical plan を
再構築し、supplied plan record の human-visible field 全てが再構築 plan と厳密一致し `plan_id` HMAC
が一致する時のみ実行する。forged / recomputed / drifted / wrong-secret / unknown-key / display 不一致は
`plan_unauthorized` とする。trusted secret 未設定・空・空白のみは fail-closed。

ordered steps (実行順は機械的依存で確定する: `scaffold apply` は installed preset を要求するため
`rules install` を先に実行する):

1. onboarding receipt を atomic write し `adoption_in_progress` を記録する。receipt は trusted
   secret 鍵の HMAC で署名し、read 時に検証する (署名不一致・未署名は broken → blocked)。
2. 選択した store に `rules install` を実行する。
3. `scaffold apply <preset> --target <root> --backup` を既存 use case 経由で実行する。
4. `.mozyo-bridge/config.yaml` を typed write-once tool で作る。
5. `workspace register` を実行する。
6. `scaffold status`、config reload、workspace inspect、herdr preflight を検証する。
7. receipt を `complete` に更新する (`complete` は全 step settled のみ)。

backend launch は本 US の step ではない。bare `mozyo` entry / launch は #13497 (conversation
provider / bare entry hook) が所有し、本 tool は launch を実行・主張しない。

config write は `{version: 1, terminal_transport: {backend: herdr}}` の typed record を排他 create
(temp + `os.link`) で確定する。file 不在は create、typed-equivalent は no-op、その他の既存 config は
上書きせず `existing_config_requires_separate_merge` で停止する。check-then-replace の TOCTOU 窓を
持たず、race で負けた場合は existing を再読して no-op / fail-closed に確定する。LLM に YAML を生成・
merge させない。

各 step は idempotent で、失敗時も完了 step と原因を credential-free receipt に残す。`onboarding.resume`
は 1 call = 1 pending idempotent step を実行する。自動 rollback で user file を消さない。backup と
resume を標準 recovery にする。

同一 root のapply/resumeはroot-scoped OS lockを取得した一つのrunnerだけが実行する。lockを
取れないrunnerはmodelを起動せず `onboarding_locked` と既存runnerを待つnext actionを返す。
process crashでOS lockが解放された後はreceiptからresumeする。別runnerがlockを強制破棄したり、
並行applyをmergeしたりしない。

## Bare entry flow

```plantuml
@startuml conversational_onboarding
|CLI / Preflight|
start
:bare mozyo;
:inspect cwd and existing markers;
if (adopted and complete?) then (yes)
  :normal backend-aware launch;
  stop
endif
if (hard block?) then (yes)
  :render reason and safe next owner;
  stop
endif
if (sync/cloud caution?) then (yes)
  |Human|
  :acknowledge non-Git adoption caution;
  if (declined?) then (yes)
    stop
  endif
endif
|Conversation|
:ask only unresolved intent;
:emit OnboardingIntent;
|Orchestrator|
:validate closed schema;
:build drift-bound plan;
|Human|
:confirm visible mutation plan;
if (declined?) then (yes)
  stop
endif
|Deterministic tools|
:apply one idempotent step at a time;
if (failed?) then (yes)
  :record credential-free receipt and next action;
  stop
endif
:verify and mark receipt complete (backend launch is #13497, not this tool);
stop
@enduml
```

## Verification contract

- pure tests: preflight matrix (home / normal / sync / symlink ambiguity / Git / non-Git)、schema reject、
  human gate receipt spoof拒否、plan再inspect / drift、config write-once、step resume、同一root lock、
  no credential persistence。
- scenario: fresh non-Git sync fixtureで bare `mozyo` → caution確認 → flagなし会話 → config / scaffold /
  registry / herdr slot ready。git init は実行されない。
- regression: adopted tmux / herdr project の bare launch byte-invariant、#13379 home refusal、壊れた
  config fail-closed、explicit subcommand は不変。
- live: owner shellで env 未設定かつ PATH 上の herdrを解決し、fresh targetを一度だけadoptする。
- E2Eは #13490 で human / coordinator / gateway / worker の入口をまとめて監査する。
- path safety classifierは #13498 配下のtask-level例外としてorchestrator配線前に独立監査する。

## 参照正本

- `vibes/docs/logics/bootstrap.md`
- `vibes/docs/tasks/external-project-herdr-adoption.md`
- `vibes/docs/rules/public-private-boundary.md`
- `vibes/docs/logics/turnkey-e2e-acceptance.md`
- `vibes/docs/specs/herdr-native-identity.md`
