# delegated coordinator smoke test frame

Redmine #12474 / parent #12454。`Delegated Coordinator / Nested Handoff`
runtime の受入確認を、重い実機 smoke だけに依存しないための test frame。

## 背景

#12474 の再試験では、GK3500 parent coordinator から mozyo_bridge delegated
coordinator を経由し、孫 implementation lane/window が visible cockpit 上に形成される
かを確認した。実行には fresh pane 準備、Redmine sparse target 作成、handoff、
receiver の Redmine read、route observation、projection 照合、contamination 判定が必要
で、1 回あたり 30 分前後の運用負荷がある。

この負荷の smoke を開発中の主検証にすると、profile / route / lane creation / metadata
stamping のどこが壊れたのかを後から切り分けることになる。実機 smoke は最後の薄い
受入確認に寄せ、失敗しやすい契約は classical tests で先に弾く。

## 実機 acceptance 正本との境界

外部親 project が抽象依頼から mozyo_bridge child を能動選択し、子が孫 implementation
lane を起動する full real-machine acceptance の正本は
`vibes/docs/logics/delegated-coordinator-real-machine-acceptance.md` に置く。

本書は、その acceptance を実機 smoke だけに依存させないための test frame である。ここには
classical tests へ落とす契約、実機に残す最小確認、過去の failure mode の分解だけを置く。
親 prompt にどの hint を渡してはいけないか、autonomous parent smoke と context-rich smoke
の PASS/FAIL 境界は acceptance 正本を参照する。

現在の repo-local 実行順序は #12499 の roadmap anchor と
`delegated-coordinator-real-machine-acceptance.md` の `Current Roadmap Gates` を読む。
#12546 autonomous parent smoke は、#12590 source/test layout full expansion、#12608
queue-enter default rail dogfood、scenario / oracle rerun/audit の後に実行する。

## #12474 / #12484 / #12485 で分かった failure mode

### context-rich contamination

#12484 では handoff summary を sparse にしても、receiver が #12474 management context を
読んだため context-free smoke としては contaminated になった。これは receiver が賢すぎる
問題ではなく、Redmine を source of truth とする運用では「関係 issue を探す」行動が自然に
起きるという test harness 側の問題である。

context-free / bounded-read smoke を名乗るなら、read boundary を機械的に狭めるか、読める
surface 自体を小さくする必要がある。読んではいけない issue id を列挙するだけでは、その
列挙自体が hint になる。

### bounded-read parent surface contamination

#12485 では bounded-read contract を守った receiver が、direct parent description だけを
読むつもりで Redmine detail を取得したところ、tool response に parent journals / children が
含まれたため `contaminated` として停止した。これは boundary enforcement としては PASS だが、
bounded-read smoke としては INVALID である。

この failure は runtime route の不具合ではなく、test harness が clean read surface を
提供できていない問題である。

### profile / operation disconnect

role profile の部品は存在する。`coordinator`, `delegated_coordinator`,
`implementation_gateway`, `implementation_worker` は固定 profile として定義され、
`handoff send --role-profile ...` で展開できる。

ただし、#12474 の実送信では `--role-profile` 付きの route は観測されなかった。また
`delegate-launch-adopt` / `delegate-grandchild-dispatch` は read-only decision primitive
で、profile 付き recommended command を出すが、その command を実行して lane/window を
作成・採用・stamp・send する orchestration ではない。

### same-lane fallback

grandchild lane が未実体化のまま delegated coordinator が same-lane Claude に渡ると、
`parent -> child -> worker` の route は replayable に見えるが、`child -> grandchild` の
visible lane/window acceptance は満たさない。この状態は display PASS ではなく、
`grandchild_required_but_not_realized` として blocked にする。

## Classical tests へ落とすべき層

次の契約は tmux / Redmine 実機 smoke なしで検証する。

### Pure domain

- delegation policy の effective gate。
- grandchild dispatch decision の outcome (`dispatch_adopt`, `dispatch_launch`,
  `no_dispatch`, `fail_closed`)。
- active child lane count / depth ceiling / hidden subagent 禁止。
- realization gate の verdict (`realized`, `same_lane_ok`, `blocked`)。
- `KIND` / `DEPTH` / `PARENT` の derivation と invalid chain rejection。

### CLI parser / command synthesis

- `delegate-launch-adopt` が delegated coordinator 用の `--role-profile
  delegated_coordinator` を含む recommended command を生成する。
- `delegate-grandchild-dispatch` が grandchild gateway 用の `--role-profile
  implementation_gateway` を含む recommended command を生成する。
- same-lane Claude dispatch へ進む前に grandchild realization gate が実行される。
- gate が `blocked` の場合に worker handoff command を生成しない。
- `--profile-field` の quoting と unresolved placeholder reporting。

### Handoff delivery record

- `handoff send --role-profile ...` の delivery record が `role_profile`,
  `profile_source`, `profile_version`, `unresolved_placeholders` を持つ。
- pane notification は single-line pointer に留まり、完全な resolved contract は durable
  record 側に出る。
- profile 未指定は明示 fallback として記録され、暗黙 profile 推測をしない。

### Discovery / projection fixture

実 tmux の代わりに `agents targets` 相当の TargetCandidate fixture を使い、次を確認する。

- grandchild candidate がないと `dispatch_launch` または `blocked` になる。
- grandchild candidate が 1 つだけあり、repo/lane/role が一致すると `dispatch_adopt` になる。
- ambiguous candidate は fail-closed。
- stamp 後の synthetic discovery row から `KIND=implementation_worker`, `DEPTH=2`,
  `PARENT=<delegated coordinator lane>` が出る。

### Redmine read-boundary harness

Redmine MCP 実体に依存せず、receiver に渡す issue surface を fixture 化する。

- sparse issue body / journals だけで parent delegation decision を出せるか。
- parent description only surface で足りない場合に `read_boundary_insufficient` を記録するか。
- parent journals / sibling / recent issue が混入したら `contaminated` として PASS/FAIL を出さないか。

## 実機 smoke に残すべき最小確認

classical tests を通した後、実機 smoke は次だけを見る。

- #12590 / #12608 / scenario-oracle rerun が pre-smoke gate として満たされており、その記録が
  Redmine #12499 / #12546 から追跡できる。
- fresh panes / worktrees が stale evidence なしで用意される。
- `delegated-coordinator-real-machine-acceptance.md` が定義する test model
  (`autonomous_parent`, `bounded_read`, `context_rich`) が Start Gate で明示されている。
- parent -> delegated coordinator -> grandchild gateway の actual handoff が submit-complete する。
- Redmine journal から route / creation-or-adoption / stamp / callback が replay できる。
- live `agents targets` が expected `KIND` / `DEPTH` / `PARENT` を表示する。
- bounded-read / context-rich のどちらの model で走ったかを明示し、contaminated run を
  PASS/FAIL に混ぜない。

実機 smoke は e2e acceptance であり、unit / integration test の代替ではない。実機 smoke
で初めて profile disconnect や missing actuator を発見する状態は遅すぎる。

### GK3500 rerun harness boundary

#12698 の GK3500 探索 smoke は、route authority、role binding、contract refs、callback rail
の欠落が混ざると、失敗理由を receiver の判断ミスとして誤分類しやすいことを示した。修正後
rerun は #12709 を test issue とし、#12709 description と
`delegated-coordinator-real-machine-acceptance.md` の `GK3500 ticketless rerun gate` を
開始前に読む。

product evidence として認めるもの:

- grandparent が Redmine smoke issue / prior journal / expected route を読まず、routing
  metadata だけで project gateway を分類または fail-closed する。
- parent project gateway が明示 role と workflow contract を受け取り、domain/design 判断を
  自身でも grandparent でも吸収せず、Redmine anchor 境界を作って child coordinator へ橋渡しする。
- child / grandchild へ進む場合、Redmine anchor、callback target、no-dispatch reason が
  durable record で replay できる。
- callback / hands-off が pane 上の自然文ではなく、standard transport または durable anchor
  で caller lane へ戻る。

assisted hypothesis check に留めるもの:

- operator が absolute doc path、role payload、manual Enter、pane focus、debug `%pane` を補って
  receiver の理解だけを観測する run。
- `mozyo-bridge message` / marker failure 後に raw text が composer に残った状態から続ける run。
- transport gap を手作業で避けて role-binding だけを確認する run。

assisted run は有用なら Redmine に記録するが、#12709 の product rerun PASS にはしない。
同じ session 内で contaminated / assisted state が混ざった場合は、fresh session / fresh unit
からやり直す。

## 推奨実装方針

1. まず route orchestration を pure plan と side-effect executor に分ける。
   - plan: Redmine anchor / policy / discovery fixture から、必要な command sequence と
     durable records を生成する。
   - executor: worktree/window/lane 作成、stamp、handoff send を行う。
2. plan 層に classical tests を厚く置く。
3. executor 層は fake tmux / fake Redmine provider で command order と fail-closed を検査する。
4. 実機 smoke は nightly / explicit QA gate 相当の薄い確認にする。

## 合格条件

本 frame を満たす状態は次の通り。

- #12474 相当の実機 smoke を走らせる前に、classical tests が profile / dispatch /
  realization / read-boundary / projection の主要 failure を弾ける。
- 実機 smoke の failure は「環境・実送信・live projection」の問題として扱え、domain /
  profile / planner の基本契約 failure ではない。
- smoke run の model (`context-free`, `bounded-read`, `context-rich`) と contamination
  classification が Redmine journal から再現できる。

## 参照正本

- `vibes/docs/logics/delegated-coordinator-cockpit-display.md`
- `vibes/docs/logics/delegated-coordinator-real-machine-acceptance.md`
- `vibes/docs/logics/ticketless-project-gateway-runtime-ux.md`
- `vibes/docs/specs/delegation-policy-project-config.md`
- `vibes/docs/specs/delegated-coordinator-role-profile.md`
- `vibes/docs/logics/role-profile-handoff-expansion.md`
- `vibes/docs/specs/delegated-coordinator-decision-records.md`
- `vibes/docs/logics/coordinator-sublane-development-flow.md`
- `skills/mozyo-bridge-agent/references/workflow.md`

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/logics/delegated-coordinator-smoke-test-frame.md --repo . --format text`
