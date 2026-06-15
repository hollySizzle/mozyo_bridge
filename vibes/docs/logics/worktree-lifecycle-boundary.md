# Worktree lifecycle は core ではなく skill / runbook で扱う

Redmine #11889。git worktree の生成・削除・命名・並列運用 policy を mozyo-bridge core CLI の標準機能にせず、LLM が実行できる skill / runbook / operator recipe として扱う方針正本。

> 本 doc は責務境界の **方針正本 + 汎用 runbook** である。新しい core CLI worktree command は追加しない。実運用 policy (削除条件・N 本並列の上限など) は operator 判断であり、OSS default に固定しない。

## 背景

`#11850` の cockpit サブレーン PoC では、issue ごとに git worktree を作り、lane / pane / branch / Redmine gate を対応させる運用が有効だった (運用哲学は [[logic-cockpit-sublane-operating-model]])。

一方で `git worktree add/remove`、issue 番号からの branch/path 強制生成、削除 policy、N 本並列運用 policy まで mozyo-bridge core に取り込むと、core が **agent / session / handoff の identity・discovery・safety primitive** から **Git workflow manager** へ肥大する。`#11889` はこの肥大を避ける境界を固定する。

## 中核方針

```yaml
core_responsibility:
  scope: identity / discovery / safety primitive のみ
  worktree_lifecycle: 含めない (skill / runbook / operator recipe で扱う)
```

worktree lifecycle (作成・削除・命名・並列 policy) は **LLM が実行できる手順** として skill / runbook / operator recipe 側に置く。core は worktree を「作る/消す」のではなく、既にある checkout / lane の **事実を観測し、危険を warning する** ところまでを担う。

## core に含めてよいもの (identity / discovery / safety)

- checkout path / git common dir / branch / repo root / `lane_id` の検出・表示 (lane identity は `#11820`)。
- same workspace + different lane の識別 (同一 workspace の複数 checkout を別 lane として扱う)。
- `agents targets` の lane / repo / role / pane 表示 (compact target discovery は `#11811`)。
- `--target-repo auto` などの target repo preflight。
- dirty worktree / stale installed CLI / ambiguous target の warning。

これらは「既に存在する checkout / lane の事実」を読む read-only / safety primitive であり、worktree の生成・破壊を伴わない。

## core に含めないもの (worktree lifecycle)

- `git worktree add` / `git worktree remove` の公式 lifecycle orchestration。
- issue 番号から worktree path / branch name を強制生成する仕様。
- worktree 削除 policy の標準化 (いつ・どう消すか)。
- N 本並列開発の operator policy (上限・命名規約・退役順序など)。

これらは project / operator ごとに異なる判断を含むため、core CLI の標準機能ではなく runbook / recipe で扱う。

## worktree サブレーン運用 runbook (汎用・LLM 実行可能)

以下は core 機能ではなく **operator recipe** である。具体 path / repo 名は環境依存なので placeholder で書く。private な絶対 path・社内 repo 名・operator 固有 lane policy を OSS default として固定しない。

```text
# 1. サブレーン用 worktree を作る (core ではなく素の git。path/branch は operator 判断)
git worktree add <worktree-path> -b <branch>      # 例: ../<repo>-<issue>  issue_<issue>

# 2. cockpit に lane として append し、role を bind する (ここから core primitive)
mozyo cockpit ...            # lane append (詳細は cockpit / lane docs)
mozyo-bridge init claude     # / codex。registry-aware adoption + role bind (#11427)

# 3. lane / repo / role / pane を確認する (discovery primitive)
mozyo-bridge agents targets --session <cockpit-session>

# 4. 作業・handoff・gate は Redmine journal を durable source に行う
#    pane message は durable anchor への pointer にすぎない

# 5. 退役: dirty 状態を確認してから worktree を消す (削除可否は operator 判断)
git status --short                                # in-scope dirty が無いことを確認
git worktree remove <worktree-path>
```

runbook の原則:

- worktree の add/remove は **素の git** または operator recipe で行い、mozyo-bridge core command にしない。
- core primitive (`init` / `agents targets` / handoff / `--target-repo` preflight) は lane の **identity と安全な配送** にのみ使う。
- 削除前に dirty / in-scope 変更を確認する safety step は runbook 側の必須手順とし、core の自動削除にしない。
- 具体 path / branch 命名は operator が決める。issue 番号からの強制生成を runbook の前提にしない。

## sublane retirement authority

Redmine #11959。sublane の退役は、原則として main coordinator Codex の運用責務である。owner は成果物・release・権限・破壊的な project state の判断者であり、routine な lane 清掃のたびに owner 承認を求めると、sublane 運用が owner の手作業に依存してしまう。

ただし、これは無条件の削除権限ではない。coordinator は退役前に objective な check を実施し、結果を durable record に残す。条件を満たす退役は coordinator authority で実行でき、条件外は owner または設計判断へ escalate する。

### coordinator が owner 確認なしに退役してよい条件

次をすべて満たす場合、coordinator は owner 確認なしに pane kill / worktree remove / local branch delete を実行してよい。

- 対象 issue / child issue が close 済み、または当該 lane の作業範囲が durable record 上で明確に完了している。
- lane の commit が main / release branch に到達済み、または `git cherry -v <base> <branch>` 等で patch-equivalent と確認できる。
- worktree dirty state が空、または残差が明確に disposable な local runtime state だけである。
- active な review request、owner approval wait、blocked handoff、unread callback が残っていない。
- 退役対象 pane / worktree / branch が lane identity と一致しており、別 issue の作業場を巻き込まない。

disposable local runtime state の例:

- `.claude/settings.local.json` の許可 command 追加など、lane 内 Claude の一時的な local permission state。
- editor / tmux / local tool が作った再生成可能な cache や preview artifact。

これらは repository artifact でも durable source of truth でもないため、上記条件を満たす lane では coordinator が破棄してよい。

### owner / design escalation が必要な条件

次のいずれかがある場合、coordinator は自律退役しない。

- 未 push / 未 merge / patch-equivalent 未確認の commit がある。
- dirty diff の scope が不明、または source / tests / docs など成果物候補を含む。
- credential、token、個人情報、private path、operator-specific secret が含まれる可能性がある。
- issue が owner judgment、release approval、credential / permission、destructive project state を待っている。
- active review / blocked consultation / unresolved callback が残っている。
- worktree path / branch / pane identity が曖昧で、別 lane を消す可能性がある。

この場合は durable record に退役できない理由と next action を残し、owner / auditor / implementer の適切な actor に戻す。

### retirement record

material な退役では、対象 issue に以下を記録する。

- retired lane / branch / worktree / pane id。
- pre-retirement checks: issue state、commit reachability / patch equivalence、dirty state、active gate absence。
- 実行した command の要約。
- 破棄した local-only state がある場合、その分類。
- 退役後の `agents targets` / `git worktree list` / `git status` の確認結果。

この record は owner approval ではなく operational audit trail である。routine retirement は coordinator authority で閉じるが、release / credential / destructive project state の owner approval requirement は緩めない。

## 将来設計への判断材料

- **`#11813` event/timeline backend**: handoff / event の timeline を扱う際も、worktree の作成・削除を backend の責務に取り込まない。timeline は lane / pane / gate の **事実の記録** に留め、worktree lifecycle manager 化しない。
- **`#11887` overlay scaffold / governance 配布**: overlay 配布に worktree lifecycle orchestration を混ぜない。overlay は配布物の scope であり、worktree 運用は runbook scope。両者を混同しない (本 issue では `#11887` を混ぜない)。
- 配布物最小化の方針は [[logic-scaffold-distribution-minimization]] と整合させる。worktree runbook は repo-local docs であり、配布される shared skill / preset 本体には入れない。

## private / operator policy 分離

- runbook 例は generic に保つ。personal な絶対 path・private repo 名・operator 固有の並列 lane policy を OSS default に混入させない ([[rule-public-private-boundary]])。
- 社内固有の削除条件・並列上限・命名規約は private operating policy 側に置き、本 repo-local doc には generic な手順骨子のみ残す。
- shared skill (`skills/mozyo-bridge-agent/**`) は全 downstream に配布されるため、worktree サブレーン runbook を shared skill 本体へ入れない。repo-local logic doc (本 doc) に置く。

## scope 境界 / Design Consultation triggers

次を要する変更は本 issue に含めず、個別 issue + Design Consultation で行う:

- `git worktree add/remove` の core CLI lifecycle command 追加。
- `#11820` lane identity semantics の変更。
- private / operator 固有の並列 lane policy を product default へ追加。
- shared skill の挙動を、互換 story なしに全 downstream へ影響する形で変更。
- `#11887` overlay scaffold / governance 配布 scope を本 issue に混ぜる。

## 検証

- `mozyo-bridge docs validate --repo .` ほか catalog 検証一式 (本 doc の catalog 登録時)。
- `mozyo-bridge docs validate --check-file-coverage --repo .` (logics coverage root に本 doc を含めるため)。
- `mozyo-bridge docs generate-file-conventions --repo . --check`。
- `mozyo-bridge docs resolve vibes/docs/logics/worktree-lifecycle-boundary.md --repo . --format text` で関連 docs 解決を確認。
- shared skill / plugin mirror は変更しないため `scripts/sync_plugin_skill.sh --check` は drift を出さない (no-op 確認)。
