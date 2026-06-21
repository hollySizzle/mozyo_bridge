# サブレーン帯域 / admission policy

## 目的

この文書は、管制塔が追加 sublane を開くべき時、既存 lane の drain を優先して
dispatch を止めるべき時、そして lane retirement が cockpit throughput にどう影響
するかを定義する repo-local policy である。

本 doc は [[logic-coordinator-sublane-development-flow]] と
[[logic-worktree-lifecycle-boundary]] を補完する。これらの文書は lane の役割、
順序、retirement authority を定義する。本 doc は **管制塔の帯域判断**だけを扱う。

## 原則

sublane bandwidth は CPU capacity ではなく、管制塔の注意力である。実用上の default
は pipeline-first であり、管制塔が drain すべき coordinator-owned queue を持たない
間は、独立した実装 work を止めずに進める。

lane は、管制塔が durable state を読み、仕様判断を route し、audit し、owner approval
を集め、local state を retire する必要がある時に bandwidth を消費する。単に
`implementing` の lane よりも、待機中 lane の方が review / close / release /
retirement を止めるため高コストになることがある。

効率的な並列開発は明示的な目標である。durable state 上 ready な implementation work
があり、下記 admission check を満たすなら、管制塔は sublane を積極的に使う。
すべての work を main lane に直列化することは cockpit model を無駄にするため、
default ではなく throughput smell として扱う。既に `implementing` の lane がある
ことは positive pipeline occupancy であり、管制塔が idle になる理由ではない。

一方で、pane や worktree を作れるというだけで work を開いてはならない。管制塔は
callback を受け、必要な audit を実施し、完了 lane を durable state を失わず retire
できる場合に限って dispatch する。

また、並列化が総 latency や risk を増やす場合は意図的に直列化する。例は、未決の
design decision、file / invariant overlap、管制塔だけが drain できる review /
owner decision、release / credential / destructive-operation gate、別 lane を見えなく
する callback backlog である。

## Lane State Classes

bandwidth 判断では、すべての lane を durable record から次のいずれかに分類する。
pane layout だけから状態を推測しない。

- `implementing`: local Claude が durable issue / journal に基づいて実装中。
- `callback_due`: dispatch は行われたが、期待される callback または durable gate が無い。
- `review_waiting`: implementation_done / review_request があり Codex audit が必要。
- `owner_waiting`: review / close flow が main coordinator Codex 経由の owner approval を必要とする。
- `close_waiting`: review / owner close approval / integration disposition または明示的 no-integration 判断は記録済みだが、Redmine issue status がまだ open。
- `integration_waiting`: review / owner close approval は満たされ得るが、commit-bearing implementation について target branch merge、CI / merge 用 push、target branch との patch-equivalent、または branch / commit owner 付き explicit deferral が未記録。
- `blocked`: blocker、design consultation、failed handoff、未解決 dependency が記録されている。
- `retire_ready`: work は integrated または patch-equivalent、issue scope は完了、active gate は残っていない。
- `idle`: active durable work が無く、reuse または retire できる。

`callback_due`、`review_waiting`、`owner_waiting`、`integration_waiting`、
`close_waiting`、`blocked` は coordinator-blocking state である。optional new work を
開く前に drain する。close-ready issue が `着手中` のまま残る状態は harmless な
bookkeeping ではなく、durable state の不整合であり、sublane が active なのか retire
ready なのかを隠す。同様に、closed issue に unmerged local sublane commit しか無い
状態は drain 完了ではない。実装は存在するが、target branch / CI / release path を
issue から再構築できない。

`implementing` は coordinator-blocking state ではない。local soft profile の lane
count には数えるが、それだけでは次の dispatch を止めない。active set が
`implementing` lanes と coordinator だけなら、管制塔の期待 action は独立した ready
work を探して pipeline に載せることである。この状態で直列実行を選ぶなら、具体的な
durable reason が必要である。

## Admission Rule

implementation-shaped work に Implementation Request を出す前に、管制塔は dispatch
decision を記録する。受信者が既に開いている main-unit Claude であっても同じである。
この decision を省略すると、管制塔 lane が黙って implementation lane へ変わるため
process gap になる。

decision には次を記録する。

- work が implementation-shaped か coordinator-only か。
- sublane dispatch が default route か。
- sublane dispatch を使わない場合、main-lane / default-lane work の方が安全または速い具体例外。
- current active lane count と coordinator-blocking queue。
- dispatch を止める場合の次 drain action。

implementation-shaped work では sublane dispatch が default である。Main-unit Claude
は read-only investigation、summary / draft、design consultation preparation、
durable reason 付き urgent minimal correction、または明示的 owner / operator decision
の例外に限る。「pane が既に開いている」は理由にならない。

新しい sublane を dispatch する前に、管制塔は次を記録または確認する。

- target issue、target lane、branch / worktree identity、durable dispatch anchor が既知。
- work が implementation-shaped であり、main coordinator lane / main-unit Claude が担うべきではない。
- 未読の `review_request`、`owner_waiting`、`integration_waiting`、`close_waiting`、`blocked`、`callback_delivery_failed` が coordinator action を待っていない。
- 開く lane について、次に必要な review / owner aggregation / retirement を管制塔が実施できる。
- 別 active sublane と file、invariant、release-critical surface が実質的に重ならない。重なる場合は ordering / merge plan が記録済み。
- production、release、credential、destructive-operation、owner decision gate が active な時に lower-priority optional item を開いていない。
- local soft profile を超える `retire_ready` lane がある場合、退役済みまたは保持理由が記録済み。

いずれかが満たせない場合は、追加 sublane を開かない。blocking state を記録し、先に
drain する。

すべて満たし ready implementation work がある場合、dispatch が preferred action である。
ready work を残して管制塔が止まる場合、または default-lane Claude に直接渡す場合は、
その状態で直列実行の方が効率的または安全である理由を記録する。

### Pipeline Dispatch Check

待つかどうかを決める前に、次の quick classification を使う。

- `review_waiting`、`owner_waiting`、`integration_waiting`、`close_waiting`、`blocked`、`callback_due`、`callback_delivery_failed` があれば、まず coordinator-owned queue を drain する。
- 既存 active lanes が `implementing` のみで、新しい work が独立しているなら、local soft profile 内で別 sublane を dispatch する。
- 独立性が不明なら、疑われる overlap を記録し、bounded read-only investigation または明示的 serialization decision のどちらかを選ぶ。黙って待たない。
- 管制塔が otherwise idle なのに待つ場合、journal に dispatch を unblock する条件を書く。

### Post-Dispatch Fill Loop

pipeline-first は dispatch 前の一回限りの admission ではない。管制塔は、次の各時点で
active lane set を再分類し、local soft profile まで pipeline を埋めるか、止める理由を
durable record に残す。

- sublane dispatch が 1 本成功した直後。
- callback / review / owner / integration / close / retirement を drain した直後。
- owner-facing next action を提示する前。
- 「次にやるべきタスク」を判断する前。

この loop では、まず coordinator-blocking state を drain する。coordinator-blocking state
が無く、`implementing` lane だけが active で、独立した ready implementation work が
残っており、local soft profile に余力があるなら、管制塔は次の sublane を dispatch する。
1 本目の dispatch が成功したことは stop condition ではない。

ready work が残っているのに追加 dispatch しない場合、次のいずれかの durable reason を
record する。理由なしの待機、pane 上の雰囲気、または「いま 1 本動いているから」は
invalid である。

- `stop_no_ready_work`: ready implementation work が無い。
- `stop_overlap`: file / invariant / merge order の衝突があり、直列化が安全。
- `stop_coordinator_blocking`: review / owner / integration / close / blocked / callback_due を先に drain する。
- `stop_soft_profile_full`: local soft profile の target / burst / stop 条件に達している。
- `stop_owner_or_release_gate`: owner decision、release、credential、destructive-operation gate が active。

post-dispatch fill は無制限 dispatch ではない。soft profile、overlap、owner / release gate、
callback backlog、retirement cadence は維持する。変えるのは default の停止条件であり、
「1 lane が implementing 中」は停止条件から外す。

## Drain Order

複数 lane が attention を必要とする場合、durable issue により強い依存が無ければ次の順に扱う。

1. production / release / credential / destructive-operation blockers。
2. 管制塔だけが集約できる `owner_waiting`。
3. `review_waiting`。
4. commit はあるが merge / push / patch-equivalence / explicit deferral が未記録の `integration_waiting`。
5. durable close gates が満たされた `close_waiting`。
6. `blocked` または `callback_due`。callback delivery failure を含む。
7. cockpit / worktree attention を消費する `retire_ready` lane。
8. new dispatch。

この順序は coordinator bandwidth のためのものであり、Redmine gate、review quality、
owner close approval separation を変更しない。

## Local Soft Profile

mozyo_bridge dogfooding では次の repo-local soft profile を使う。

- target: main coordinator に加えて active implementation sublane 3 本。
- burst: active implementation sublane 4 本目は、既存 review / owner / blocker / retirement queue を飢えさせない理由を管制塔が記録した場合のみ許容。
- stop: 5 本目以降の active implementation sublane は、explicit owner / operator decision を durable issue に記録せず開かない。
- cleanup: lane count が target を超える場合、次の optional dispatch batch の前に `retire_ready` lane を退役する。

これらの数字は portable な mozyo-bridge core default ではない。downstream project は別の
private operating profile を定義してよい。portable rule は上記 admission / drain model
と、burst decision を durable ticket system に記録する requirement である。

## Retirement Cadence

routine retirement は [[logic-worktree-lifecycle-boundary]] に従い coordinator-owned である。

bandwidth control としては次を守る。

- lane count が local target を超えている場合、close 後すぐ `retire_ready` lane を退役する。
- それ以外でも、次の dispatch batch を開く前に `retire_ready` lane を退役する。
- lifecycle checks が通るなら、owner が明示的に cleanup を頼んでいなくても closed lane を cockpit に残し続けない。
- dirty state unknown、unresolved callback、active review、owner wait、blocker、identity ambiguity がある lane は retire しない。

## Durable Record Template

target 超過 dispatch または bandwidth full による dispatch stop では、短い journal を記録する。

```markdown
## Sublane dispatch decision

- current_lanes:
  - <issue>: <state>
- coordinator_blocking_states: <none | list>
- active_implementing_lanes: <none | list>
- ready_independent_work: <none | issue list>
- capacity_remaining: <number within local soft profile>
- work_shape: <implementation | coordinator_only | read_only | design_consultation>
- admission_decision: <dispatch_sublane | stop_and_drain | burst_dispatch | main_lane_exception>
- post_dispatch_fill_check: <done | not_applicable>
- fill_decision: <dispatch_next | stop_no_ready_work | stop_overlap | stop_coordinator_blocking | stop_soft_profile_full | stop_owner_or_release_gate>
- reason: <why this decision is safe; if serializing, the concrete dependency or overlap>
- next_drain_action: <review | owner aggregation | blocker | retirement | none>
```

journal には issue ID と state class を書き、private path や operator-specific cockpit
details は書かない。

## Non-Goals

- `git worktree add/remove` orchestration を mozyo-bridge core に追加しない。
- private cockpit layout、personal path、operator-specific staffing assumption を OSS default に焼かない。
- bandwidth decision を理由に Redmine gates、Codex review、owner close approval、Codex gateway rule を免除しない。
- main coordinator lane または main-unit Claude を、既に開いているという理由で substitute implementation lane にしない。

## Verification

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/logics/sublane-bandwidth-policy.md --repo . --format text`
