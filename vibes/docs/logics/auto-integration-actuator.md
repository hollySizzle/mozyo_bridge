# Guarded auto-integration / retirement-cleanup actuator

Redmine #13686 (parent #12603 / Version #303)。coordinator が手作業で行っていた
「review 承認 → integration branch への統合 → CI → close → lane 退役 (managed process の解放)」を、
**gate 付きで replayable な単一 actuator** に移すための設計正本。lane の **worktree 削除と local
branch 削除はどちらも本 actuator の scope から外れた** — 経緯と根拠は `## 破壊的操作を持たない理由`。

owner decision は #13686 j#96335、設計境界は同 j#77124 (Coordinator Design Answer,
approved_with_corrections)。両者が本 doc の上位である。実行契約のうち **authority 側**
(誰が統合してよいか、どの gate を満たす必要があるか) の正本は central preset
`agent-workflow.md` `### Commit Hash Origin 到達可能性` の `coordinator_owned_auto_integration`
であり、本 doc はそれを複製せず、**この repo の実装がその契約をどう構成しているか**を固定する。

> 本 doc は責務境界 + 実装構成の正本であり、gate 語彙・close 条件・review authority の正本
> ではない。矛盾した場合は central preset と Redmine journal を優先し、本 doc を是正する。
> 正本分離は [[rule-llm-rule-authoring]] `## 正本分離` に従う。

## 背景 — なぜ「自動 merge 禁止」を撤回したか

#11889 / [[logic-worktree-lifecycle-boundary]] は、worktree lifecycle を core CLI に取り込むと
core が **identity / discovery / safety primitive** から **Git workflow manager** へ肥大すると
判断し、境界を固定した。central preset も同じ趣旨で「自動 merge / auto-integration 機構の導入」
を全面禁止していた。

owner は j#96335 でこの全面禁止を **範囲が広すぎる** と判断した。撤回されたのは *機構の存在*
の禁止だけであり、次の 2 点は動いていない。

- **実装者は integration branch を前進させない。** 実装者の push は issue / lane branch に限る。
- **統合は coordinator の責務である。** actuator は coordinator の明示操作を代行する実行系で
  あって、新しい authority ではない。

したがって本 actuator が足すのは *権限* ではなく、**同じ権限で行われていた手作業の再現性**
である。手作業だった頃に暗黙だった各 revalidation を、明示的な gate と durable な段階別 outcome
に変えることが主目的である。

## 二つの state machine を分ける (j#77124 必須訂正1)

現行 #12604 `SublaneIntegrationUseCase.evaluate_retire` は `issue_closed` / callbacks drained /
durable retire record を **merge の前に** 要求する。ここへ live merge を単純接続すると、実際の
順序と逆転する。

```text
実際の順序:  review approval → integration → exact-SHA CI → task/US close → retire
#12604 の形: issue close + callback drain → merge → retire      ← 逆転
```

そこで #13686 は 2 つの machine を分離する。両者は state を共有せず、integration 側は close を
行わず、cleanup 側は merge / push を行わない。

```text
[integration]  integration_preflight → (integration_apply) → push_waiting → awaiting_ci → integrated
                                                                                    ↓
                                                                      (issue close は別経路)
                                                                                    ↓
[cleanup]      cleanup_preflight → process_retiring → retired
```

`integration_preflight` / `cleanup_preflight` は **entry phase であり resting state ではない**。
preflight は「失敗 gate を見つけて blocked になる」か「後続 state へ渡す」のどちらかで、
`preflight のまま止まっている` という状態は存在しない (consumer が進捗と誤読するため、test で
固定している)。

### terminal disposition

| disposition | 判定根拠 | 副作用 |
| --- | --- | --- |
| `integrated` | push 済み + 統合 SHA の CI green (**gate は外せない**) | push |
| `already_integrated` | target ancestry (source head が target から到達可能) | なし |
| `patch_equivalent` | **明示的な patch-id evidence** | なし |
| `not_applicable` | 非 Git workspace | なし (process retire は別途走る) |
| `disabled` | `mode: disabled` (既定) | なし |
| `integration_blocked` | いずれかの gate 不成立 | なし |

`already_integrated` と `patch_equivalent` を分けるのは j#77124 必須訂正2 の要求である。前者は
ancestry という機械的事実、後者は evidence を要する主張であり、両者を畳むと durable record が
持っていない ancestry を主張してしまう。どちらも **同じ merge を再生成しない**。

## action record と idempotency (j#77124 必須訂正2)

一つの統合行為は immutable な action record に束ねる。`action_key` はその 6 field そのものである。

```yaml
action_key:
  - issue
  - lane_generation
  - source_head            # full 40-hex commit SHA (branch 名は pin ではない)
  - target_ref
  - expected_target_head   # 存在しない target は `none` sentinel
  - review_generation      # approved review_request の exact journal id
```

step ledger は action key ごとに記録され、`done` の step は再実行しない。6 field のいずれかが
drift すれば **別の key** になるため、古い ledger が新しい action を満たすことはない。これが
「部分失敗から再実行しても duplicate merge / delete を起こさない」の実体である。

`review_generation` は caller が自由に命名する世代 label ではない。最新の correlated approval が
承認した `review_request` の **exact journal id** である。production composition は ledger / action
registry を開く前に durable journal からこの値を再構成して action record と照合し、actuator も
registry の writer mutation 直前と各 action-time preflight で再照合する。したがって、未承認の
文字列や composition 後に supersede された request を action key に入れて新しい世代を作ることは
できない。composition が返す mutation capability も、そこで承認した exact
action key だけでなく workspace / lane / branch / worktree / repo root を含む durable resume frame
全体へ pin される。正しい record で composition した後に別 frame を registry へ差し替える経路も
拒否する。

段階別 outcome は `done` / `not_applicable` / `blocked` / `pending` の 4 値で、
「走った」「そもそも該当しない」「拒否された」「まだ決着していない」を畳まない。

## fail-closed の一覧

action-time に再検証し、一つでも欠ければ副作用の **前** に停止する。全 gate を集めてから報告する
ので、durable record には最初の 1 件ではなく失敗 gate の全集合が載る。

```yaml
integration:
  - foreign_worktree / unknown_target_branch
  - target_not_configured               # 設定された integration branch と exact 一致しない (R2)
  - review_generation_inadmissible      # 最新 generation が approved かつ blocking finding なし
  - source_mutated_after_review         # review 済みの exact head であること
  - source_head_unreachable             # origin 到達可能
  - unpushed_unique_commits / dirty_worktree
  - source_ci_not_green / source_ci_evidence_incomplete / source_ci_head_mismatch  # (R3)
  - unresolved_owner_gate / unresolved_callback
  - target_drift                        # expected_target_head からの drift
  - non_fast_forward                    # ff-only 時
  - merge_conflict                      # merge commit disposition 時
  - push_rejected
  - push_outcome_head_missing           # push done なのに着地 head が無い。fallback しない (R3)
  - push_head_mismatch                  # 記録された head が disposition の着地 head でない (R3)
  - integration_ci_evidence_incomplete  # run / check identity / head を欠く (R2)
  - integration_ci_head_mismatch        # push が着地した head と別 commit の run (R2)
  - integration_ci_failed               # 決着したが non-success
cleanup:
  - action_key_mismatch                 # 別 action の authorization を継承しない
  - issue_not_closed / integration_unconfirmed / integration_ci_unsettled
  - unresolved_callback / unresolved_owner_gate
  - lane_identity_mismatch              # record が自 lane (issue / generation / branch / path) を指していない
```

**代替手段を持たない**ことが安全性の中身である。conflict / non-ff / target drift / push 拒否を
rebase や force で解消しない。actuator の port は弱い操作しか公開していないため、「強い形に
fallback する」という選択肢が構造上存在しない。

## 破壊的操作を持たない理由

cleanup 側は **git を一切呼ばない**。3 つの破壊的 step を実装し、3 つとも同じ 1 文で撤去した。

> **安全性を自分自身で enforce できない操作は、「既定 off」で持つのではなく持たない。**

| 撤去 step | 必要条件 | 撤去理由 (すべて git 2.50.1 実測) |
| --- | --- | --- |
| remote branch delete (R1 / j#96344 F1) | remote tip に対する CAS | 非 force な CAS が存在しない (`--force-with-lease` は j#96335 が禁じる force)。加えて local delete を off にすると条件評価ごと飛ばして実行できた |
| local branch delete (j#96396 F1) | ref tip == record 済み source head / どの worktree も保持していない | **両軸を 1 invocation で満たす primitive が無い**。`update-ref -d <ref> <tip>` は tip を CAS するが保持中の worktree ごと消して `HEAD` を壊す / `branch -D` は保持を原子的に拒否するが tip 制約を取らない (2 個目の引数は別の branch 名) / `update-ref --stdin` は同一 ref への `verify`+`delete` を拒否。2 invocation 形は窓で着地した commit を全 ref から到達不能にした |
| worktree remove (j#96401 F1) | clean / registered / **自 lane のもの** | `git worktree remove` は path で対象を指し、clean と registered は同一 invocation で見るが **identity は見ない**。identity は別 probe だったため、その間に同じ path へ差し替えられた foreign lane の checkout を削除した。`worktree remove` に expected-identity 引数は無く、admin entry 名は差し替え後に再利用されるので instance identity にならない。`worktree lock` は path→entry の binding を pin する (competitor の remove を拒否、prune は skip、`rm -rf` 後の re-add も拒否) が、**lock 保持中は remove も move も実行できない** (`-f -f` 必要) ため、直前の unlock が窓を開け直す。lock 自体も誰でも reason 無しに unlock できる |

残ったのは `release_process(issue, lane_generation)` **1 つだけ**であり、これは偶然ではない —
**primitive 自身が identity を引数に取る**ため、決定と実行の間に対象がすり替わる窓が無い。
path と ref 名は late-bound であり、それを mutation より前に束縛するものは保証ではなく検査である。

lane の worktree / branch の削除は **operator の runbook step** として `preflight_sublane_retire`
に残る (`git worktree remove` と `git branch -d`。人間が判断する)。

上記の測定は散文ではなく **executable test** として置いてある
(`tests/integration/.../test_auto_integration_live_git.py`)。将来の git が答えを変えたら test が
知らせ、撤去の裁定を記憶ではなく証拠で見直せる。

## merge を checkout から object へ (R10 / j#96406 F1)

破壊的操作 3 件と違い、**merge には代替 primitive があった**ので撤去せず置換した。

旧: dedicated worktree を測定 → その path で `git switch <target>` → `git merge --no-ff`。
**測定と mutation の間に path を foreign lane の clean checkout へ差し替えると、その checkout が
target branch へ switch され、そこに merge commit が作られる** (実測。`conflicted=False` を返す)。
non-force push と exact-SHA CI が gate するのは **remote への着地**であって、既に他 lane の
checkout に対して行った switch / merge を取り消さない。

新: `git merge-tree --write-tree <expected_target_head> <source_head>` → tree、
`git commit-tree <tree> -p <target> -p <source>` → merge commit。
checkout・index・ref・HEAD を一切触らない (実測)。**merge の対象指定に late-bound な名前が
介在しない**ので、差し替えるものが無い。

> ⚠️ **この節は R10 時点の記述であり、2 点が誤っていた。** 訂正は下の 2 節が正本である。
> 1. R10 は「**入力は全て object id**」と断定した。**誤り** — `commit-tree` は host の identity
>    config と現在時刻を、`merge-tree`/`commit-tree` は `i18n.commitEncoding` と merge driver を
>    暗黙に読む (j#96412 F1 / j#96417 F1、いずれも実測)。
> 2. R10 は失敗を「**rc=1 のみ conflict、それ以外の非 0 は primitive 不可用**」と分類した。
>    **誤り** — `merge-tree` は **missing object も rc=1** で返し (実測)、また不可用性を exit code
>    から推定してはならない (j#96412 F2 / j#96417 F3)。

これに伴い dedicated worktree の apparatus 一式 (`IntegrationWorktree` / 専用 probe /
`integration_worktree_inadmissible` / constructor field / report field) を撤去した。残った
`describe_lane_worktree` は **lane 側の read probe** であり、「ここで mutate してよいか」ではなく
「source は review されたものか」に答える。

## 統合 commit の policy (R11-R12 / j#96412 F1・F3 / j#96417 F1・F3・F5)

object-level merge は plumbing であり、`git merge` と **同値ではない**。差分を暗黙にせず policy として明示する。

### 決定性: 何を enforce しているかを正確に述べる (F1)

契約は「**同一 git version の下で、同一 repository 内容に対し、同一 action は同一 commit を
再構築する**」である。git binary そのものは pin できないため、version 条件は契約の一部であり
省略できない (R18 まで本節だけがこの条件を落としており、同 doc の current contract 節と矛盾していた
— j#96461 F4)。
R11 が書いた「入力は arguments のみ / どの host でも同じ」は **不正確だった**。到達までに 2 度
訂正している:

| 隠れた入力 | 影響 | 対処 |
| --- | --- | --- |
| host の git identity config | 別 SHA (j#96412 F1 実測) | **固定 literal identity** を env で与える |
| 現在時刻 | 1.1 秒差で別 SHA (同上) | 下記 timestamp 規則 |
| `i18n.commitEncoding` | encoding header が付き別 SHA (j#96417 F1 実測) | `-c i18n.commitEncoding=UTF-8` で invocation ごとに pin |
| global / system config 全般 | 同上 | `GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM` を空にして実行 |
| `merge.directoryRenames` / `merge.renames` / `diff.renames` | **merged tree が変わる** (実測。j#96422 F1) | **`-c` で pin** (値は git の documented default) |
| rename limit 2 key | 我々の scene では差が出なかったが **bound が host ごとに変わるのは varying input** | `-c` で固定値へ pin |
| `merge.renormalize` / `merge.default` | merge の canonicalization と既定 driver を変える (j#96428 F3。`merge.default=union` は conflict を merged にした) | **`-c` で pin** |
| user / system attributes | merge attribute の供給元 | `core.attributesFile` を空へ pin + `GIT_ATTR_NOSYSTEM=1` |
| 継承 `GIT_*` env (`GIT_DIR` / `GIT_OBJECT_DIRECTORY` / `GIT_ALTERNATE_OBJECT_DIRECTORIES` / `GIT_CONFIG_COUNT` 等) | `-c` の対象外で git の読む先を変える | **環境を継承せず置換**する (許可 list のみ)。**dict を作ることと child が受け取ることは別** (j#96428 F1) |
| `refs/replace/*` | object を差し替える (j#96422 F1)。**timestamp probe 経由でも commit を変える** (j#96428 F2) | `--no-replace-objects` + `GIT_NO_REPLACE_OBJECTS=1` を **決定性に寄与する全 invocation** へ |
| **`merge.<name>.driver`** | **merged tree の内容が任意 code で書き換わる** (実測: conflict が rc=0 + `DRIVER WON`) | **sandbox で不可視化** |
| **`$GIT_DIR/info/attributes`** | merge 挙動を選ぶ。tree の一部でなく、どの option も redirect しない | **sandbox で不可視化** |
| **`$GIT_COMMON_DIR/shallow`** | 同じ object ids の ancestry 解釈を変える (実測: `refusing to merge unrelated histories`) | **sandbox で不可視化** |
| partial clone / promisor remote | missing object の demand fetch = **外部 I/O / availability input** | **列挙のみ** (valid object content は OID に束縛されるため別 commit 生成は未確認) |
| **git binary / version** | pin 不能 | **claim を「同一 git version」へ限定し、exact version を durable outcome に記録**。`merged` は version を記録できたときのみ成立し、ledger も non-empty を要求する (j#96441 F4) |
| **sandbox 構築そのもの** (`rev-parse` ×2 / `git init`) | 未封止なら `GIT_TEMPLATE_DIR` 等で sandbox に state を注入できる (j#96441 F1 実測) | **構築工程ごと sealed**、`--template` は agent 所有の空 dir |
| **object store の位置** | linked worktree では `--absolute-git-dir` に objects が無い (j#96441 F2 実測。本 lane がその形) | **`--path-format=absolute --git-common-dir`** から解決 |

### repo-local state は「拒否」ではなく **不可視化** する (R15 / j#96435 F1)

R12-R14 は driver と `info/attributes` を **probe して拒否**していた。**これは閉じない** —
reviewer が probe と merge の間に driver を追加し、その shell command が実行されて merged content が
書き換わることを再現した。**検査と mutation が別 invocation である限り、同じ時点に束縛されない。**
撤去した 3 つの破壊的操作と同じ構造である。

代わりに **merge を repository の外で実行する**:

- 呼び出しごとに使い捨ての bare git dir を作る (`--object-format` は実 repo と一致させる)
- `GIT_DIR` を sandbox へ、`GIT_OBJECT_DIRECTORY` を **実 repo の objects** へ向ける
- → object は全て読め、書いたものは push が見つける場所に落ちる。一方 config /
  `info/attributes` / `shallow` は **生成直後の空 repository のもの**である

実測: driver と `info/attributes` を置いたまま、直接 merge は driver の出力を含む clean tree を返し、
sandbox 経由は **通常の conflict** を返して driver は実行されなかった。shallow も同様に無効化される。

**これは precheck ではなく物理的な不可視化であり、競合窓が存在しない。** これに伴い driver /
`info/attributes` の **refusal は撤去した** — 到達し得ない入力を拒否するのは false positive であり
(j#96422 F4 と同型)、driver を常用する repo で feature を止める理由も無くなった。

**claim の成立範囲**: 「**同一 git version の下で、同一 repository 内容に対し、同一 action は同一 commit を
再構築する**」。git binary は pin できないため cross-version の同一性は主張しない。
**そして prose で限定するだけにしない** — apply の durable outcome (`StepOutcome.git_version`) に
**exact version を記録**するので、replay が同一 version 下だったかを後から照合できる (j#96435 F4)。

> **enforce 範囲は「具体名の列挙」で書く。** R10 は「入力は全て object id」、R11 は「arguments alone」、
> R12 は「pin 不能なのは driver だけ」、R13 は上の表 (2 key と 1 供給元が欠落)、
> R14 は「pin できないものは拒否した」(**拒否が precheck であり時点を束縛していなかった**) と書き、
> **5 回とも外した** (j#96412 / j#96417 / j#96422 / j#96428 / j#96435)。
> **列挙形式にしても網羅性は保証されない** — 形式は「何を主張していないか」を読めるようにするだけである。
> **さらに: 列挙して pin / refuse を並べても、refuse が別 invocation なら守っていない。**
> R15 の sandbox はこの系列で初めて **「入力を数える」ことに依存しない**構造である
> (repo-local state は分類ではなく丸ごと不可視になる)。
> 「全部やった」ではなく「pin した key はこれ、isolate した env はこれ、refuse する条件はこれ」と書く。
> 上の表がその列挙であり、**表に無いものは enforce していない**。

**driver 拒否の範囲は裁定事項ではなくなった** — R15 で refusal 自体を撤去したためである (j#96435 F1)。
driver も `info/attributes` も **sandbox から見えない**ので、宣言の有無で feature が止まることはない。

- author / committer identity = **固定 literal** (`mozyo-bridge auto-integration <auto-integration@mozyo-bridge.invalid>`)。
  `git log` 上でも人間が書いたのでないことが読める
- author / committer date = **`max(source, target)` の committer date** (j#96417 F5)。両者とも
  action key が覆う object の性質なので決定的であり、かつ **merge commit が first parent より
  古くならない** (source 単独だと non-ff の通常ケースで古くなり、`git log --since` から落ちる)
- message = 固定 format `Merge <source_head> into <branch>`
- `commit.gpgsign` は `commit-tree` に届かない (実測) が、将来届くようになっても効かないよう pin する

### hook を実行せず、署名しない (F3)

`git merge` は `pre-merge-commit` / `commit-msg` hook を実行し、`-S` で署名できる。plumbing の
`commit-tree` はどちらも経由しない。**これを「同値」として扱わず、意図的な policy とする**:

- **local hook を実行しない。** hook は host ごとに任意の code であり、(a) actuator の決定性と両立せず
  (hook が commit を書き換えれば SHA が変わる)、(b) host によって統合結果が変わることは
  「同一 action → 同一 SHA」と矛盾する。**統合成果物の gate は push 後の exact-SHA CI** であり、
  それは local hook より強い (共有 CI が同じ commit を検査する)
- **署名しない。** 署名鍵は host / 個人に紐づき、fixed identity と決定性に反する
- **この判断は project policy であり実装詳細ではない。** 署名や hook 実行を要求する運用なら、
  決定性との両立方法 (例: 決定的な署名 identity) を含めて owner/design 判断が要る

## 現行 contract の要約 (R12 時点)

歴史的経緯は後続の節に残すが、**現時点で成立している契約**はこれだけである。矛盾したら本節を優先する。

- `run_integration(record)` / `run_cleanup(record)` は **action record (identity) のみ**を受け取る。
  caller preflight も caller ledger も存在しない。
- 安全事実は **actuator が測る**: git 事実は port probe、durable 事実は `DurableAuthorityReader`、
  lane identity は actuator 自身の `lane_issue` / `lane_generation` / `lane_worktree` /
  `lane_branch`。**live reader は #14825 で実装済み** (`application/auto_integration_live_authority.py`
  + pure fold `domain/auto_integration_authority.py`)。reader は自 lane の
  `issue` + `lane_generation` に束縛され、それ以外を名乗る action record は journal を読む前に拒否する。
- 測定は **step ごと**に取り直す。integration 側は actuator 自身の mutation が世界を変えるため、
  cleanup 側は durable authority が他者によって動くため。
- target head は **fresh remote tip**。pre-push は expected-head CAS、post-push は landed-head
  reachability。
- **merge は object から組む** (`merge-tree --write-tree` + `commit-tree`)。checkout・index・ref・
  HEAD を一切触らず、first parent は measured remote target。target ref は **refspec 安全性検査と
  **literal `git check-ref-format refs/heads/<name>` を sealed env で** の両方を通ること
  (どちらも他方を包含しない。j#96422 F3。`--branch` は `@{-n}` を repository state から展開するため
  validator ではない — j#96447 F1)。control 文字を含む ref は process argv に渡せないため事前に
  `invalid_input` (j#96453 F2)。ref の検査は **spelling そのもの**に対して行い、adapter は trim
  しない — R18 は trim してから検査したため `'ma in'` を拒否しつつ `' main '` / `'main\n'` を
  黙って `main` へ書き換えていた (j#96461 F2)。**周囲 whitespace の trim は record 生成時の
  `normalized_branch` が 1 回だけ行う** upstream の工程であり、adapter 層ではない。
  さらに **typed 拒否は apply path だけでは足りない**: actuator は apply の前に必ず target ref で
  remote tip を読む (j#96461 F2 — R18 は read から例外が抜けて run 全体が落ちた)。
  **使用不能な ref は、それを取る全 operation が自身の fail-closed 値で返す**:
  read は `""` / `False`、`apply_merge` は `invalid_input`、`push_non_force` は
  **`invalid_input`** (spawn 0)。`_UnsafeRefspecError` は adapter 内部の signal であり `__all__` にも無く、
  **ref 名に起因する例外は adapter の外へ出ない**。
  **`PushResult` は closed vocabulary 1 値**であり bool の組ではない (j#96516 F1)。
  `accepted` / `invalid_input` (使用不能な source head・target ref) / `remote_moved` /
  `remote_refused` / `operational_error` / `unrecognized_status`。`accepted` と `rejected` は
  **status からの派生 property** なので、`accepted=True, rejected=True` のような**矛盾は
  validation で弾くのではなく書けない**。
  **`rejected` は `remote_moved` 以外を意味しない。**
  > R19-R21 はここで 3 度誤った。「raise する理由は戻り値が無いから」(j#96492 F4)、
  > 「2 state はどちらも試行済み / apply が先に拒否するので到達不能」(j#96499 F1)、そして
  > **「非 SHA source を `accepted=False,rejected=False` にしたのだから 3 state で足りる」** —
  > だが同じ method が **全 non-zero exit を `rejected=True`** にしていたため、
  > **spawn 失敗が「remote が動いた。action を組み直せ」として durable record に残っていた**
  > (j#96516 F1 実測)。`PushResult(accepted=True, rejected=True)` も構築でき、consumer は成功と読んだ。
  > 教訓は「置き換えた根拠を実装に当てて確かめる」「bool を足して state を増やしたと呼ばない」。

- **push の失敗は exit code から推定しない。`git push --porcelain` の自 ref 行で分類する**
  (j#96516 F1)。実測 (git 2.50.1):

| 状況 | rc | 自 ref の porcelain 行 | status | recovery |
| --- | --- | --- | --- | --- |
| 着地 | 0 | `*\t<sha>:refs/heads/x\t[new branch]` | `accepted` | — |
| remote が前進 | 1 | `!\t...\t[rejected] (fetch first)` | `remote_moved` | 新 target head で action を組み直す |
| remote が拒否 (hook 等) | 1 | `!\t...\t[remote rejected] (...)` | `remote_refused` | policy 所有者に当たる |
| remote 到達不能 | 128 | **自 ref の行なし** | `operational_error` | 環境を調査 |
| git が起動しない | 127 (`_run` sentinel) | 出力なし | `operational_error` | 同上 |

  `[rejected]` と `[remote rejected]` は**部分一致ではなく token 先頭一致**で判定する (後者は前者を含む)。
  自 ref の行が無い結果は remote の ref について何も述べていないので、**述べたことにしない**。

- **push status は durable な `StepOutcome.push_status` に載り、ledger integrity がそれを読む**
  (`push_status_step`)。push の `done` は **`push_status == accepted` のときだけ**信じられ、
  それ以外 (失敗 status / 未知 / 空) と、**push 以外の step が push status を持つ**場合は
  `ledger_push_status_unsound` で blocked。merge status と同一構造であり、同じ誤り
  (「field に載せただけで誰も読まない」j#96422 F2) を 2 度目に繰り返さないための gate である。
  **dedicated integration worktree は存在しない** (j#96406 F1)。
  **commit は「同一 git version の下で、同一 repository 内容に対し」再構築可能**であり、
  hook 非実行・無署名 (上節)。
- **merge の失敗は typed status** で、**durable な `StepOutcome.merge_status` field に載り、
  ledger integrity がそれを読む** (j#96417 F2 / j#96422 F2)。apply の `done` は
  **`merge_status == merged` のときだけ** push authority になり、それ以外 (失敗 status / 未知 /
  空) と、**apply 以外の step が status を持つ**場合は `ledger_merge_status_unsound` で zero-push。
  *field に載せただけで誰も読まなければ gate ではない* — R11 は prose に着地させ、R12 は field に
  着地させて consumer を書かなかった。vocabulary は domain 側の closed set:
  `merged` / `content_conflict` / `primitive_unsupported` / `probe_error` / `invalid_input` /
  `nondeterministic_merge_config` / `sandbox_error` / `merge_error` / `commit_error` /
  `unrecognized_status`。`sandbox_error` は「隔離を構築できなかった / object store を特定できなかった」
  であり、非決定的 config の検出ではない (j#96441 F3)。
  **sandbox lifecycle の 3 段階は、構造を分けるだけでなく 3 段階とも typed に着地する**:
  setup 失敗 = `sandbox_error`、**body (merge 実行中) の operational 失敗 (`OSError`) = `merge_error`**、
  teardown 失敗 = `sandbox_error`。**優先順位は teardown > body** — sandbox が残っている事実の方が operator が
  対処すべき事実だからである。R17 は 3 段階を 1 つの `@contextmanager` に入れて
  `generator didn't stop after throw()` を出し、R18 は分割したが body の例外を変換せず raw
  `OSError` が caller へ抜けた (j#96461 F1)。**構造分割は typed 着地の代わりにならない。**
  一方で **`ValueError` はここで捕捉しない** (j#96492 F2)。process 境界は `_run` の内側にあり、
  NUL in argv の `ValueError` は `_run` が既に失敗 `CompletedProcess` (rc=127) へ変換している。
  したがってこの handler に届く `ValueError` は operational ではなく **program defect** であり、
  捕捉すると「破られた invariant」を durable ledger へ `merge_error` として記録してしまう。
  **defect は typed status に化けさせず落とす。**
  **exit code だけで分類しない** — missing object は content conflict と同じ rc=1 で返る (実測)。
  conflict は tree を名乗り、operational failure は名乗らない。可用性は **version probe** で確かめ、
  **probe 自体が失敗したら `probe_error`** であって不可用ではない (j#96417 F3)。
  vocabulary 外の値は `unrecognized_status` として **typed に** 落とす。
- `already_integrated` / `patch_equivalent` は **push 前のみ** terminal。push 後は exact-SHA CI を完走。
- **cleanup 側に破壊的操作は 1 つも無い**。ref delete も worktree remove も持たない (上節)。
  cleanup は git port を **read probe すら呼ばない**。
- **verify する値と mutate する値を一致させる**。cleanup の identity 照合は
  `issue` / `lane_generation` / `branch` / `worktree_path` の 4 値すべてで、そこには
  `release_process` が引数に取る 2 値が含まれる (j#96406 F2)。
- `mode` は `auto` / `disabled` のみ。CI gate は config で外せない。

## 実装構成

```text
domain/auto_integration_records.py       pure: 2 machine が共有する value object 群
domain/auto_integration_policy.py        pure: mode gate / integration 状態遷移
domain/retirement_cleanup_policy.py      pure: close 後の cleanup 状態遷移 (git 操作を持たない)
domain/auto_integration_journal.py       pure: durable record renderer (判断はしない)
domain/auto_integration_authority.py     pure: journal → review / integration / CI の durable fact (#14825)
application/auto_integration_actuator.py port (Protocol) + use case + config→policy 変換
application/auto_integration_ports.py     port (Protocol) 定義 + 交換する value object
application/auto_integration_live_ops.py live subprocess adapter (実 git)
application/auto_integration_refspec.py  ref spelling 検査 + porcelain 自 ref 行の push 分類
application/auto_integration_live_authority.py live DurableAuthorityReader (#14825)
application/auto_integration_ledger.py   durable append-only step ledger (SQLite, #14825)
application/auto_integration_action_registry.py immutable action frame + continuation events (#14825 R2)
application/auto_integration_ci_source.py GitHub Actions の current exact-workflow verdict (#14825 R2)
application/auto_integration_process_ops.py live ManagedProcessOperations (#14825)
application/auto_integration_composition.py production composition root + async CI 継続 (#14825)
application/auto_integration_reconcile.py crash で取り残された admission の recovery (#14825 R2)
application/cli_workflow_auto_integration.py `workflow auto-integration` runtime entrypoint (#14825 R2)
application/auto_integration_supervisor.py scheduled continuation owner (#14825 R2)
```

`auto_integration_refspec.py` は R22 (j#96516 裁定 2) で `live_ops` から分割した。責務は
**「ref 名が何であってよいか」と「push についての git の答えが自 ref について何を意味するか」**の 2 つで、
どちらも *process boundary が運ぶ文字列* についての問いであり、repository / remote / sandbox を必要としない。
関数はすべて private (`_UnsafeRefspecError` / `_checked_branch` / `_push_status`) で、
**subsystem 内でのみ import される。public re-export はしない** (`__all__` にも入れない)。
分割の直接の契機は、typed push status 追加で `live_ops` が 998/1000 行に達したことである。

### bool ではなく記録で受ける (R1 review j#96344)

R1 は 4 つの入力を bare bool / bare string で受けており、review が「**bool は監査できない**」と
指摘した。いずれも identity を持つ record へ置換した (正本: `auto_integration_records.py`)。

| R1 | R2 | 何が言えていなかったか |
| --- | --- | --- |
| `integration_ci_green: bool` | `IntegrationCiEvidence` | どの run の・どの required check が・どの commit について green か。無関係な green run が gate を満たしていた |
| `coordinator_confirmed: bool` | (R4 で mode ごと撤回) | 誰が・どの action を・どこに記録して承認したか |
| `integration_worktree: str` | (撤去) | R1 は非空 string、R2 は測定済み `IntegrationWorktree`。**R10 で概念ごと撤去** — path を gate しても差し替えられる (j#96406 F1)。merge は object から組む |
| `policy.integration_branch` (未参照) | decision が exact-match を要求 | 設定した branch が実際に統合先を制約すること |
| `source_ci_green: bool` (R2 まで残存) | `IntegrationCiEvidence` | 同上。sibling gate に同じ穴が残っていた (R3) |

CI evidence は **push が着地した head** (ledger の push outcome が記録した commit) と exact-match
する。fast-forward なら source head、merge commit なら merge した commit である。**着地 head が
記録されていない場合に source head へ fallback しない** — 「何が着地したか記録し損ねた」ことは
「source が着地した」証拠ではない (R2 review j#96350 finding 2)。

### 型を足すだけでは足りない — 測定者を固定する (R2 review j#96350)

R1 で bool を型へ変えた 4 入力のうち 2 つは、**値を caller が供給し続けていた**ため R2 でも
自己申告のままだった。forged な `IntegrationWorktree(is_lane_worktree=False)` (当時) も、存在しない anchor を
指す `CoordinatorConfirmation` も、そのまま通った。

> **safety fact を測るのは actuator であり、依頼者ではない。**

R3 ではこの 2 つを preflight の入力から外し、actuator が action-time に自分で測る。

R3 は 2 field だけを測り、残りを caller から取っていた。R3 review j#96368 finding 1/2 が
「**2 項目だけ測っても、残りが caller 供給なら mutation authority は依然 caller のもの**」と指摘し、
cleanup 側では **foreign lane の worktree 削除と branch 削除**が caller boolean だけで再現された。

**R4 で caller preflight を廃止した。** `run_integration` / `run_cleanup` は preflight 引数を持たない。
caller が渡すのは action record (identity) と、この actuator 自身の lane 設定だけである。

| 事実の種類 | 誰が測るか |
| --- | --- |
| git 事実 (target head / ancestry / dirty / registered / tip / checked-out / origin 到達) | actuator が `AutoIntegrationGitOperations` の read probe で測定 |
| durable 事実 (review generation・reviewed head・target identity・callback・owner gate・CI) | actuator が `DurableAuthorityReader` port から action-time に読む |
| lane identity (これは自分の lane か) | actuator 自身の `lane_worktree` / `lane_branch` と照合 |
| patch equivalence | **測定できない**。明示 evidence を要する主張であり、probe が無いので提供しない |

authority reader 未注入なら durable 事実は何も確立されず、`integrated` にも `retired` にも到達しない
(fail-closed)。cleanup は record の path/branch が **actuator 自身の lane と exact 一致**しない限り
`lane_identity_mismatch` で止まる — CAS tip 一致は「branch が動いていない」ことしか言わず「それが自分のものか」を
言わないためである。

### 測定は step ごとに取り直す (R5 review j#96385 findings 2/3)

**一度だけ測った snapshot は、その後のすべての mutation にとって stale である。** しかも actuator が
作用する世界は **actuator 自身の mutation が変える** 世界である。R5 まではこれを取り違えていた:

- push が成功すると remote target は `expected_target_head` から landed head へ移る。それを
  pre-push の期待値と比べていたため、**自分の成功を drift と誤判定して resume が恒常的に止まった**
  (feature が完了不能だった)。→ **pre-push は expected-head CAS、post-push は landed-head が
  現在の target から到達可能か**、と質問を分けた。到達不能は `integration_lost_from_target`
  (「誰かが先に動かした」= drift とは別の事実、「我々の成果が消えた」)。
- `already_integrated` は **push 前にのみ** terminal disposition である。push 後は source が target
  から到達可能なのは当然であり、そこで終了すると exact-SHA CI gate を飛ばしてしまう。
- **cleanup 側はもう世界を動かさない** (破壊的 step を全て撤去したため)。それでも step ごとに
  durable authority を読み直す — owner gate や callback は *他者* が動かすからである。この節が
  扱っていた `branch_checked_out_elsewhere` / remove 前後の checkout 状態は、対応する step と
  共に撤去された probe である。

> **test double が mutation の効果を反映しないと、検査そのものが無効になる。**
> R5 の 2 件はどちらも、fake が push 後も target head を静止させ、remove 後も checkout 状態を
> 固定していたために test をすり抜けた。mutating port の fake は自分の mutation を世界へ適用する。

### ledger は provenance と順序を持つ (R3 review j#96368 finding 3)

`StepOutcome` は `recorded_by` を持ち、actuator は **自分が記録した entry しか数えない**。
さらに mutation の前に `ledger_integrity_errors` が dependency order と必須 head を検査する。
push を apply より前に記録した ledger では、R3 は apply だけ実行して **push せずに `integrated`** へ
到達していた。merge の push は自 run の apply が生んだ commit を押す。source head への fallback は
**decision 層と mutation 層の両方から**除去した (R3 は decision 層しか直していなかった)。

### `coordinator_confirmed` mode は提供しない (R3 review j#96368 finding 4)

R3 は confirmation resolver を port として置いたが production binding が無く、mode は live 実行不能で、
任意の injected resolver が架空 anchor を保証できた。reviewer が示した 2 択のうち「配線完了まで
mode を非提供にする」を採った。**follow-up が実装すべき契約**は次のとおり: anchor を action-time に
fresh-read し、その記録が **この exact action key** を confirm していることを確認し、`issuer_role` は
**記録の author から導出**する (caller が名乗った role は authority ではない)。

domain は IO を持たず、事実は全て caller が preflight として渡す ([[logic-object-oriented-architecture-policy]]
の pure core 方針、既存 `domain/sublane_integration_policy.py` と同じ形)。use case は
**decision が authorize した step だけ**を 1 回ずつ実行し、outcome を ledger へ積む。

CI は actuator が actuate しない。統合 SHA の CI は非同期 gate であり、use case は
`pending` を記録して停止する。workspace callback supervisor が永続 action registry から同じ frame を
発見し、**landed SHA + target branch + required workflow** の provider verdict を poll する。success の
ときだけ同じ action を再入し、
live authority reader が durable marker と provider の current verdict を再測定して terminal へ進める。
failure は `ci_failed`、pending / unavailable は `awaiting_ci` のままである。「run が決着した」と
「run が green だった」は別の事実である。

## 設定 (`.mozyo-bridge/config.yaml` の `auto_integration`)

```yaml
auto_integration:
  mode: disabled            # auto | disabled (既定 disabled)
  integration_branch: null  # 未設定 = target 未設定 (何も統合しない)。設定時は action の target と exact 一致必須
  ff_only: true             # 既定 (j#96335)
```

`integration_branch: null` は **runtime 解決への委譲ではない** (#14825 item 6 で当該宣言を撤回した。
詳細は下記 `## live composition (#14825)`)。

**post-close cleanup の key は 1 つも存在しない** (`remove_worktree` / `delete_local_branch` /
`delete_remote_branch`)。対応する step を全て撤去したため (上記「破壊的操作を持たない理由」)、
宣言すると unknown key として fail-closed する。**無い操作を off にする key は持たない。**

**CI key も存在しない。** R2 は `require_source_ci` / `require_integration_ci` を持ち、
「j#96335 が『branch/target CI』を設定駆動項目に列挙している」ことを根拠に waiver を owner 授権済み
と主張した (dispute j#96346)。R2 review j#96350 finding 1 がこれを否とし、**私はその判断を受け入れて
dispute を撤回した** (j#96351)。理由は 2 つある。

1. **anchor の数が逆だった。** j#77124 state 5 (`integrated: origin reachability + exact-SHA CI
   green を確定`)、j#96335 自身の target flow (`... → exact integration SHA CI green → Close Gate`)、
   j#96337 の fail_closed (`CI未確定`) の 3 つが「integrated には CI green が要る」と述べている。
   「branch/target CI を設定駆動」は **どの** required check を要求するかの設定とも読め、その読みなら
   j#96335 は自己整合する。私の読みは同 journal を自己矛盾させる読みだった。
2. **waiver に downstream semantics が無かった。** cleanup は統合 SHA の CI green を常時要求する。
   waiver 後の lane は cleanup が永久 block するか、未実行 CI を green と自己申告して破壊的 step へ
   進むかのどちらかになる。end-to-end で成立しない gate は gate ではない。

既定 `mode: disabled` は **behavior-preserving** である。#13686 以前は auto-integration が存在
しなかったため、block を宣言しない repo は従来どおり完全手動の coordinator 統合を保つ。

config は **operational intent のみ**を持つ。state machine は判断時に config の field を読まない
ため、flag は step を止められても gate を外せない。閉じた key 集合には force push / rebase /
approval / review / close を表す key が構造上存在せず、boundary 形の key
(`owner` / `approval` / `review` / `close` / `route` / `send` / credential 等) は既存の
closed-schema screen が拒否する。宣言状況は `mozyo-bridge config status` の
`auto_integration.*` leaf row で読める (未宣言の実効値が推測ではなく表示される)。

## live composition (#14825)

#13686 は machine と port を作り、実装を一つも束ねなかった。#14825 がそれを束ねる。**この節が
「production で誰が何を答えるか」の正本**であり、#13686 側の記述と矛盾したらこちらを優先する。

### durable authority は共有 producer を通す (item 1)

live reader は grammar を新設しない。review generation の可否と承認 head、integration disposition は
`hibernate_basis_producer.produce_conjuncts` — auto-hibernate basis が読むのと **同一の producer** — から
取る。相関規則 (approval は自分より厳密に前にある最大の `review_request` を答え、head 一致、後続 request で
supersede) の定義を二本にしないためであり、その producer 自身が「三本目を書いた」defect の記録を持っている
(checkpoint j#86443 R2-F1)。issuer は `hibernate_issuer_policy` が committed config blob に anchor して解決する。

evidence の lane envelope は **actuator 自身の設定** (`LaneScope`) と exact 一致を要求する。producer が
target を見ないのは自身の anchor check を tautology にしないためで、比較そのものは caller 側で行う
(hibernate では T1 classifier が同じ役割を担う)。

**CI は head 単位かつ branch context 付きで読む。** source CI と integration CI は merge なら別 commit、
fast-forward なら同じ commit になり得る。同じ SHA でも issue branch の quick run と target branch (`main` 等) の
integration batch は別 run であり、前者を後者に流用しない。

- source CI: coordinator の `required_ci_green` marker が名指す exact workflow / run を source branch 上で
  provider と再照合する。
- integration CI: 同 marker が required workflow を authority として定め、provider が返す **landed SHA +
  target branch** の最新 run を evidence の run id / conclusion とする。fast-forward の同一 head に相異なる2 markerを
  作ると既存 grammar が conflict にするため、target run の事実は provider sourceから取る。
- provider query は installed `gh` でも成立する `gh api --method GET .../actions/runs -f head_sha=<sha>` を使い、
  返答の `head_sha` / `head_branch` / workflow / run id / conclusion を再照合する。後発 failure / pending は古い
  success を撤回し、unavailable は fail-closed。

`callbacks_drained` は callback outbox のうち **exact workspace + issue + owning lane** に属する
未解決債務だけを読む。ここには名前が似た二つの lifecycle 軸があるため、混同しない。

- `coordinator` route の `enqueue_lane_generation` は lane incarnation (`lane_generation`) と比較する。
- `lane_gateway` / `review_return` route の `target_generation` は lifecycle CAS `revision` と比較し、
  route suffix と `target_lane` も current lane と byte-exact 一致させる。

production reader は action-time ごとに active owner と lifecycle row を読み直す。foreign workspace /
issue、正規だが異なる過去世代・revision、route と `target_lane` が一致して証明できる別 lane は
現在 action を止めない。一方、同一 scope の空・padded・不正・競合 identity、unknown route、
legacy blank workspace、outbox / lifecycle read failure は fail-closed で止める。coordinator row は
owning lane id を保存していないため、owner 交代後に別 lane が同じ incarnation number を持つ場合は
foreign と証明できず保守的に debt のままとする。workspace-wide の未解決数をそのまま使う
hibernate 向け authority とは integration action の scope が異なる。

`owner_gates_resolved` は issue 自身の canonical gate fold (`fold_issue_gate_facts`) の `blocked` gate /
未解決 review round から読む。actuator は「この issue が止まっているか」について二つ目の意見を
持たない。

### ledger は admission に紐づく receipt でしか書けない (item 4 / R2 で修正)

`SqliteLedgerStore` は home-scoped SQLite file。

> ⚠️ **R1 の記述は誤っていた。** R1 はここに「`recorded_by` の自己申告は読まず store の `writer_id` を stamp する」
> と書き、それを authentication と呼んだ。**検査していない payload に provenance を押すのは file の認証であって
> claim の認証ではない** — review j#96611 finding 3 は、mutation 一切なしの `append` で
> `done` + `push_status=accepted` の push receipt を作り、それが cleanup の authorizing action として
> 採用されることを再現した。以下が現行契約である。

- **public store は read capability。** 通常 caller が構築する `SqliteLedgerStore` は read-only で、
  mutation capability は production composition が actuator / reconciler 内へ私有する。authorization reader は
  `AutoIntegrationLedgerReader` だけを持ち、`begin_step` / `append` / `resolve_intent` を持たない。
- **receipt は admission に紐づく。** private writer の `begin_step` が one-time token を open intent 行へ鋳造して返し、
  `append` は **その `(action_key, step)` の open intent と一致する token** を要求する。誰も admit されて
  いない step の outcome は記録できない。
- **admission は compare-and-set である。** open intent への partial unique index により、同一 step への
  2 つ目の `begin_step` は **副作用の前に** 拒否される。R1 は `unresolved_intents` を読んでから insert する
  check-then-write で、2 つの run が同じ push に admit された (j#96611 finding 4 実測)。
- **ledger 単独で cleanup を authorize しない。** accepted push receipt の landed head と、ledger writer が
  支配しない coordinator-issued integration disposition の landed head が exact 一致して初めて authorizing action
  になる。shared file が相互不信 process を暗号学的に認証するとは主張しない。
- **信頼主体は専用 ledger process ではなく、この運用では OS account である。** repository、Git remote、
  Redmine credential と ledger file を同じ OS account が保有するため、private writer surface は誤用を
  減らす API boundary であって、悪意ある same-UID code に対する security boundary ではない。same-UID
  code や SQLite 直接改変を脅威モデルへ含めるなら、ledger だけを daemon 化しても成立せず、Git /
  Redmine mutation credential を含む全 mutation authority を別 OS principal へ移す必要がある。
- **crash は検出だけでなく回復する** (j#96611 finding 5)。crash した run の token は道連れになるので、
  `resolve_intent` は token 無しで **観測した exact intent id の open rowだけ**を CAS で閉じ、resolution の
  action / step identity と settled outcome 語彙を store 側で照合してから **reconciler が測定した内容**を記録し、
  `reconciled` と印づける (「やった run がそう言った」と「後の run が見に行った」を durable record で混同しない)。
  open intent の存在は要求するので、receipt を捏造する第二の経路にはならない。
- `done` step は action key ごとに一度だけ (partial unique index)。

### crash recovery は測定で閉じる (R2)

`application/auto_integration_reconcile.py`。durable record が答えられない唯一の問い —
**mutation は着地したか** — を世界に訊き、stranded admission をその答えで閉じる。

- `push`: 提示されたはずの head (merge なら自 action の apply receipt、ff なら source head) が
  target ref から到達可能か。**到達可能=landed / 不可能=not_landed**。
- `integration_apply` / `integration_ci`: ref を動かさない (apply は object を書くだけ、CI は観測のみ) ため、
  中断は target を変えていない。`not_landed` として閉じ、再実行に委ねる。
- **`ambiguous` は不便ではなく設計である。** probe 自体が実行できない (target tip が読めない) ときは
  admission を **開いたまま** にし、action は止まったままになる。見えないときに推測する reconciliation は、
  それが回復しようとしている欠陥そのものを一層外側で繰り返す。**読めない remote は push されていない commit ではない。**

### cleanup の authorization は record 自身から来ない (item 5)

#13686 の cleanup preflight は `authorizing_action_key` を `record.integration_action_key` から埋めており、
decision はそれを同じ field と比較していた。比較の両辺が同一値なので gate は成立し得ず、record が自分自身を
authorize していた。live reader は **ledger** から答える: この issue / generation / source head の下で
`push` step を `done`(status `accepted`) で記録した action key が唯一に定まるとき、それが authorizing action である。

**問う step が `integration_ci` ではなく `push` なのは計測結果である。** actuator は CI step を `pending` としか
記録しない — evidence が決着すると decision はそれを読んで直接 terminal state へ移り、step を実行しないため
`done` の CI 行は production では一度も書かれない。CI が green だったかは durable record 側の
`integration_ci_settled_green` が答えるので、ledger には ledger が答えられる問い (どの action が commit を
publish したか) だけを問う。

### 非同期 CI は待たずに継続する (item 3)

`AsyncCiContinuation` は同じ action record で `run_integration` を再入する。通常 owner / trigger は production
`WorkspaceCallbackSupervisor` に結線した `AutoIntegrationSupervisorLeg` である。bounded reconciliation の
issue lease 内で `registered` / `awaiting_ci` action を永続 registry から発見し、landed SHA + target branch +
required workflow の current verdict を poll する。idempotency は **immutable action frame + append-only
transition + step ledger の一意制約**。
二重 wake / operator の再実行 / supervisor retry は再読み・再判断するだけで再 push できない (push は既に
`done`、二度目の receipt は store が拒否する)。1 pass / leg の external mutation は 1 件までで、callback /
reconcile / hibernate と pass-wide budget を共有する。分類は **action registry の terminal state** で行う。

### target branch は設定されているか、actuator が存在しないか (item 6)

`integration_branch: null` は「runtime 解決へ委譲」と記述されていたが resolver は一度も存在しなかった。
#14825 は resolver を作らず **宣言を撤回する**。対象は **push の target** であり、late-bound な名前で identity を
代替する形は #13686 が merge (j#96406 F1) / branch delete (j#96396) / worktree remove (j#96401) から
取り除いた当のものである。composition root は未設定の場合 actuator を **組み立てない** (例外)。

`mode` / `ff_only` / `integration_branch` は作業ツリーの未 commit ファイルではなく、current `HEAD` の
exact config blob を **decision ごとに** 読み直す。未 review のローカル編集は operational intent の authority
ではない。`target_identity_known` も同じ committed config を **呼び出しごとに** 読み直して答える。これは
`policy.integration_branch` (この instance が構築された値) とは **別の問い** であり、repository が現在宣言して
いない branch に対して構築された actuator は、その不一致で fail-closed する。

> ⚠️ **R1 はこれを contract として書き、実装は snapshot だった** (j#96611 finding 2)。branch tuple を構築時に
> 1 回だけ作って closure で閉じ込め、issuer anchor (`committed_config_policy_pointer`) も reader 構築時に
> 1 回だけ解決していた。**両方とも呼び出しごとの再読へ直した**。anchor は writer を role へ束縛する値であり、
> それを snapshot から読むのは、この subsystem が他の全箇所から取り除いている stale-authority の形そのものである。

### runtime entrypoint (R2 / j#96611 finding 1)

`workflow auto-integration <run|continue|settle|reconcile|cleanup|reconcile-cleanup>`
(`application/cli_workflow_auto_integration.py`)。R1 は composition root と continuation を作って
**どこからも呼ばれない**まま置いた (`grep` の runtime 参照 0 件)。**誰も呼ばない trigger は trigger ではない。**

- `run` — immutable action frame を mutation より先に登録し、integration machine を rest まで駆動する。
- `continue` — **同じ action** を手動再入する recovery/debug surface。authority gate は迂回しない。
- `settle` — provider を手動 poll し、terminal success のときだけ再入する recovery/debug surface。
- `reconcile` — crash で取り残された integration admission を remote の 3 値測定で閉じる。
- `cleanup` — guarded managed-process release を実行する。
- `reconcile-cleanup` — release 周辺で取り残された cleanup admission を観測で閉じる。

通常 trigger は CLI ではなく scheduled workspace supervisor である。CLI `run` は実 `CallbackOutbox` を渡し、
accepted push と required workflow を durable registry に残すため、別 process の supervisor が exact frame を
復元できる。public `SqliteLedgerStore` は read capability のみで、mutation capability は actuator / reconciler の
private composition に限定する。

**この command は authority を足さない。** 引数から identity を解決し composition root へ渡し、machine の
判断を印字するだけである。gate を skip / force / waive する flag は存在せず、**`--target-ref` も存在しない**
(caller が push の target を名指しできることは item 6 が撤回した runtime resolution そのものである)。

### live ManagedProcessOperations (item 7)

対象解決は lifecycle store の `(issue_id, lane_generation)` のみ。path / pane locator / display name は
解決に一切関与しない。lifecycle row は `(workspace, lane)` で keyed され **現行 generation** を持つので、
superseded generation は **どの row にも一致しない** — staleness に別 probe は要らない。突合は ownership check
の **前に全 workspace を横断**する: 自 lane で先に絞ると、同じ issue / generation を主張する別 lane が
見えなくなり、止めるべき曖昧さが綺麗な単一一致になってしまう。0 件 / 複数件 / foreign lane / inventory 読取不能は
すべて zero-release。mutation が消費する identity は **解決された row 自身**から作り、row の `revision` を
driver の `expected_revision` に渡す (解決と mutation の間の lifecycle write — generation bump を含む — は
close 0 で admission block になる)。

これらは fake ではなく **実 `LaneLifecycleStore`** に対する production test で固定してある
(`tests/integration/.../test_issue_14825_live_authority_composition.py`、j#96408 の条件 5)。

## scope 境界 / 未了

- ~~**CLI subcommand 結線は本 tranche に含まない。**~~ **撤回済み (#14825 R2)。** #13686 R1 時点の
  記述であり、`workflow auto-integration` の結線で解消した (`### runtime entrypoint`)。R2 はこの
  bullet を残したまま新節を足し、**同じ doc が「結線済み」と「結線しない」を同時に述べる**状態を
  作った (review j#96650 finding 6)。`application/cli_sublane_retire.py` (#14755 の保護 path) には
  依然触れていない — 新設した command は `workflow` family 側にある。
- **Herdr / Redmine / remote / ledger の live smoke は commit 後の受入手順で実施する。** unit /
  integration test の fake-free production composition と、実環境 smoke の結果を混同しない。実測前に
  live acceptance 済みとは記録しない。
- durable `required_ci_green` marker は authority attestation、GitHub Actions source は action-time の
  current factとして **積**で評価する。source CI は marker の exact run + source branch、integration CI は
  landed SHA + target branch 上の同 workflow の最新 runを要求する。後発 failure / pending は古い green を
  撤回し、provider unavailable は fail-closed。marker vocabularyを拡張せず red-survival と fast-forward時の
  source-run流用の両holeを閉じる。
- `domain/sublane_integration_policy.py` (#12604) の retire 判断は **置き換えていない**。本 doc の
  2 machine はその隣に新設したものであり、既存 `sublane retire` の挙動は変えていない。両者の
  統合 / 片寄せは別 issue の判断とする。

## 検証

- `python3 -m unittest tests.unit.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_auto_integration_policy`
- `python3 -m unittest tests.unit.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_retirement_cleanup_policy`
- `python3 -m unittest tests.unit.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_auto_integration_live_ops`
  (live adapter が構成する実 argv と typed status の分類。`_run` を stub した hermetic test で、実 git
  process は起動しない。**隔離そのものは stub の向こう側で起きるため、ここでは検査できない** —
  j#96428 F1 の教訓であり、隔離は下の real-Git suite でのみ pin する)
- R1 review j#96344 の 5 finding は `R1ReviewFindingRegressionTest` /
  `R1ReviewFinding1RegressionTest` / `NoRemoteRefDeleteTest` に、**再現した入力そのもの**で
  pin してある。verdict は j#96345。
- R2 review j#96350 の 4 finding は `R2ReviewFindingRegressionTest` および
  `CoordinatorConfirmationResolutionTest` / `MergeCommitRunTest` に同様に pin してある。
  verdict と full-surface escalation の受け入れは j#96351。
- `python3 -m unittest tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_auto_integration_actuator`
- `python3 -m unittest tests.unit.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_issue_14825_auto_integration_authority`
  (#14825 の durable fold / live reader の identity fence / target 宣言)
- `python3 -m unittest tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_issue_14825_live_authority_composition`
  (#14825 の実 `LaneLifecycleStore` / 実 SQLite ledger に対する production test。zero-release 4 条件、
  admission/receipt 境界、duplicate admission の CAS、crash recovery の landed / not_landed / ambiguous、
  非同期 CI 継続、cleanup authorization。R1 review j#96611 の finding 3/4/5 は
  `R1ReviewFindingRegressionTest` に **再現した入力そのもの** で pin してある。verdict は j#96613)
- `python3 -m unittest tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_issue_14825_auto_integration_cli`
  (6 runtime subcommand と実 callback outbox の到達可能性。j#96611 finding 1 / j#96650 finding 7)
- `python3 -m unittest tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_issue_14825_auto_integration_supervisor`
  (scheduled owner、pending→success、duplicate wake、push crash recovery、canonical root fence)
- `python3 -m unittest tests.unit.e_130_governance_distribution.f_140_rules_docs_catalog.test_auto_integration_config`
- `PYTHONPATH=src python3 -m mozyo_bridge docs validate --repo .` ほか catalog 検証一式。
- preset 本文を変えたため `PYTHONPATH=src python3 -m mozyo_bridge scaffold canonical --check` と
  `scaffold status --target .` を通す (canonical body → packaged preset → repo-local preset の
  3 段同期。詳細は [[logic-scaffold-rules]])。
