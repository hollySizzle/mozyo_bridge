# 既存プロジェクト sublane adoption runbook

Redmine #12423。既存プロジェクトへ mozyo-bridge の governed scaffold と sublane 運用を導入する時の repeatable runbook である。新規プロジェクトの bootstrap ではなく、既に code、router、ticket 運用、catalog、独自ドキュメントを持つプロジェクトを壊さず adopt するための手順を定義する。

## 目的

- 既存プロジェクトへ `redmine-governed` または同等の governed preset を導入する。
- root の scaffold-managed router / governed artifact を導入しつつ、既存の project-local routing、subdir catalog、既存 docs を不用意に上書きしない。
- adoption の判断、例外、検証、callback gap を Redmine journal から replay できる形で残す。
- 一度の consumer adoption で得た知見を次の既存プロジェクト導入へ再利用する。

## 非目標

- `mozyo-bridge` core を Git worktree manager にすること。
- consumer 固有の cockpit 構成、private path、business label を public default へ焼くこと。
- scaffold preset template や CLI enforcement をこの runbook だけで変更すること。
- governed preset を全プロジェクトへ機械的に強制すること。

## 用語

- **adoption target**: governed scaffold / sublane 運用を導入する既存プロジェクト。
- **root adoption**: repository root または workspace root に `AGENTS.md` / `CLAUDE.md` / `.mozyo-bridge/**` を置き、agent の入口を root から安定させる導入。
- **preserve routing**: 既存 subdir や project-local docs の routing を壊さず、root router から必要な正本へ辿れる状態を保つこと。
- **bootstrap 例外**: adoption 自体を成立させるため、通常なら事前に完全な child gateway decision を置く場面で、管制塔が後追い correction / progress journal を残して進める例外。通常開発の bypass 口実にはしない。

## 適用判断

この runbook を使う条件:

- 既存プロジェクトが Redmine journal を durable source of truth として使う、または今後使う方針が明確である。
- agent が継続的に implementation / review / close を行い、pane chat だけでは replay 不足になる。
- sublane dispatch、callback、Review Gate、owner close approval、integration disposition を journal で追える必要がある。
- project root に router が無い、または root router と subdir / local docs の関係が曖昧で、agent entrypoint が揺れている。

使わない条件:

- 一回限りの sandbox、短命 demo、または Redmine lifecycle を維持しない project。
- catalog owner が不在で、`catalog.yaml` / generated check を day 2 以降維持できない project。
- private operating policy を OSS default として固定しないと成立しない project。

## 役割

- **Owner**: adoption scope、close approval、release / production / destructive / credential 判断を承認する。
- **管制塔 Codex**: read-only preflight、scope 分解、sublane dispatch、audit、integration disposition、owner-facing 判断、runbook 反映を担当する。
- **target-lane Codex**: adoption target の gateway。durable anchor を読み、自 lane の Claude へ same-lane handoff し、implementation_done / review_request / blocked / owner_waiting を管制塔へ callback する。
- **sublane Claude**: bounded implementation。scaffold apply / rules install / catalog adjustment / verification を行い、commit hash と verification を Redmine に残す。

## 実行フロー

```plantuml
@startuml existing_project_sublane_adoption
start
|管制塔 Codex|
:Read Redmine parent / child issues and project docs;
:Record read-only preflight journal;
if (durable work system and governed preset are justified?) then (yes)
  :Create or identify implementation child issue;
  :Record dispatch decision and target lane identity;
else (no)
  :Record stop reason;
  stop
endif

|target-lane Codex|
:Read durable anchor from Redmine;
:Confirm adoption target identity and same-lane Claude route;
:Handoff to same-lane Claude;

|sublane Claude|
:Run scaffold/rules/catalog adoption steps;
:Preserve existing routing and project-local docs;
:Run verification;
:Commit only adoption-scope files;
:Record implementation_done and review_request;

|target-lane Codex|
:Send coordinator callback with issue, journal, state, commit hash;
:Record callback outcome;

|管制塔 Codex|
:Audit diff, journals, origin reachability, and verification;
if (commit can be used as integration anchor?) then (yes)
  :Record Review Gate and integration disposition;
else (no)
  :Record local-only / unreachable blocker;
  stop
endif
:Request owner close approval when gates are satisfied;
:Close child issues, then parent US;
stop
@enduml
```

## 手順

### 1. Read-only preflight

adoption target を変更する前に、管制塔は read-only preflight を Redmine に残す。

確認するもの:

- target project の durable work system と Redmine project / Version。
- 既存 root router (`AGENTS.md`, `CLAUDE.md`) の有無と、手編集か scaffold-managed か。
- `.mozyo-bridge/scaffold.json`、repo-local rules store、central rules store の有無。
- 既存 `.mozyo-bridge/docs/catalog.yaml` と generated file conventions の有無。
- root と subdir の関係。root adoption が subdir catalog や subproject router を上書きしないか。
- `git status --short` と current branch。unrelated dirty files は scope 外として明示する。

preflight journal には、adoption する理由、触る予定 path、触らない path、想定 preset、verification plan、known risk を書く。

### 2. Scope と issue 分解

既存プロジェクト adoption は少なくとも次へ分ける。

- parent UserStory: adoption の目的と close 条件。
- implementation child: scaffold / rules / catalog / router adoption の変更。
- verification child: dry-run、status、docs validation、handoff smoke などの検証。

implementation と verification を同一 issue に閉じると、commit-bearing work と dry-run 結果の review anchor が混ざりやすい。親 US の audit は child issue、commit、verification、callback outcome、integration disposition をまとめて読む。

### 3. Dispatch decision

implementation-shaped work は sublane dispatch を default とする。管制塔が自 lane で直接編集する場合は例外理由を Redmine に残す。

dispatch decision に最低限書くもの:

- target issue。
- target lane / branch / worktree identity。
- work_shape: `implementation`。
- expected changed paths。
- main-lane で行わない理由、または main-lane 例外の理由。
- callback expectation。

adoption bootstrap では、child gateway や target-lane docs が未整備なため、事前 decision record が薄くなることがある。その場合でも、後続 journal で「bootstrap 例外」「足りなかった decision」「次回からの必要 record」を明示する。例外は adoption を成立させるための補正であり、通常開発で gateway を省略する理由にはならない。

### 4. Scaffold / rules adoption

実装 lane は target project で次を実施する。コマンドは target の preset / central-or-repo-local 方針に合わせる。

```bash
mozyo-bridge rules install
mozyo-bridge scaffold status --target .
mozyo-bridge scaffold apply <preset> --target . --backup
mozyo-bridge scaffold status --target .
```

repo-local rules store が必要な環境では `rules install --repo-local .` と `scaffold apply ... --repo-local` を使う。central と repo-local を混ぜない。

`scaffold apply` は existing root router や governed artifacts に差分を出す。差分は必ず読んで、次を確認する。

- root router が thin router として central / repo-local preset を指している。
- `AGENTS.md` と `CLAUDE.md` の tool-specific entrypoint が同じ central workflow を参照する。
- shipped artifact は `.mozyo-bridge/scaffold.json` に tracked files として入っている。
- existing subdir router、business docs、private overlay、generated output を hand-edit していない。
- `.mozyo-bridge/docs/catalog.yaml` は target-owned data として扱い、scaffold re-apply で上書きしない。

`rules install` 後に `scaffold status` が preset hash drift を示す場合、manifest が古い central preset hash を持っている可能性がある。差分が manifest hash のみか、router / artifact 本文にも drift があるかを分けて判断する。hash-only correction でも commit と journal に残す。

### 5. Catalog adoption

governed project では catalog が day 2 の source of truth になる。初回 adoption では、shipped `catalog.yaml.example` を target project の実態に合わせて `catalog.yaml` へ採用する。

catalog adoption で確認するもの:

- coverage roots が存在する root / subdir を指す。
- root-level router / governed artifact / project docs / implementation paths に file conventions がある。
- generated file conventions は手編集せず、generator で同期する。
- existing subdir catalog がある場合、root catalog と二重正本にしない。root から subdir の正本へ辿れるようにするか、scope を分ける。
- private path、credential、operator 固有 cockpit 構成を public catalog に入れない。

### 6. Verification

最低限の verification:

```bash
mozyo-bridge scaffold status --target .
mozyo-bridge doctor --target .
mozyo-bridge docs validate --repo .
mozyo-bridge docs validate --check-file-coverage --repo .
mozyo-bridge docs generate-file-conventions --check --repo .
git diff --check
```

必要に応じて追加するもの:

- `mozyo-bridge docs resolve <changed-paths...>` で root / subdir の active docs が解けるか。
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`。
- target project 固有 test / lint / dry-run。
- same-lane handoff / coordinator callback smoke。

verification child は、commit-bearing implementation と独立に、target repo の clean status、scaffold status、docs validation、generated sync、dry-run smoke を Redmine に残す。

### 7. Commit と origin reachability

review / close anchor にする commit は origin reachable でなければならない。local-only commit は implementation observation としては読めるが、close / audit anchor としては扱わない。

確認するもの:

```bash
git rev-parse --short HEAD
git branch --contains <commit>
git branch -r --contains <commit>
```

target branch へ integration する場合、implementation branch に unrelated local history が混ざっていないかを確認する。混ざっている場合は、target branch から clean integration branch を作り、adoption scope の commit だけを cherry-pick する。Redmine には original implementation commit と integration commit の対応を書く。

### 8. Callback と recovery

sublane は handoff-worthy state に到達したら coordinator callback を送る。対象 state は `implementation_done`、`review_request`、`review_result`、`owner_close_approval_waiting`、`blocked` である。

callback が missing / misaddressed の場合は、管制塔が Redmine journal sweep で recovery してよい。ただし recovery journal に次を残す。

- どの state が durable record 上では進んでいたか。
- callback がどこに届いた、または届かなかったか。
- coordinator がどの journal から復旧したか。
- 次回の route correction。

Redmine journal recovery は強い fallback であり、nagger は品質向上の補助である。nagger や複数 Claude による機械的強制を primary control にしない。source of truth は journal、callback は pointer、nagger は secondary signal として扱う。

### 9. Review / close

管制塔 audit では次を読む。

- implementation child の Start / Implementation Done / Review Request / callback outcome。
- verification child の dry-run / status / docs validation。
- commit diff と changed files。
- origin reachability。
- integration disposition。
- bootstrap 例外や process correction の有無。

close の順序:

1. child implementation / verification issue を audit する。
2. parent US に Review Gate を記録する。
3. owner close approval を分離して回収する。
4. child issues を close する。
5. parent US Close Gate を記録し close する。

owner approval / close / release / credential / destructive gate は adoption で緩めない。bootstrap 例外は decision record の薄さを補正するだけで、approval invariant を bypass しない。

## Known pitfalls

- `scaffold status` clean は workflow adoption 完了ではない。Redmine lifecycle で実際に Start / handoff / callback / review / close が回る必要がある。
- `scaffold apply` の backup 付き上書きは安全確認であって、差分 review の代替ではない。
- `catalog.yaml.example` は shipped skeleton、`catalog.yaml` は target-owned 正本である。
- generated file conventions は手編集しない。
- local-only commit を close anchor にしない。
- implementation branch に unrelated history が混ざったら、そのまま target branch に push しない。
- same-lane Codex への通知は coordinator callback ではない。cross-lane callback outcome を journal に残す。
- bootstrap 例外を通常開発の shortcut にしない。

## Journal templates

### Read-only preflight

```markdown
## Read-only preflight

- adoption_target: <project/repo label>
- durable_source: Redmine #<parent> / #<child>
- intended_preset: redmine-governed | redmine-rails-governed | other
- existing_entrypoints:
  - AGENTS.md: <absent | present | scaffold-managed | hand-edited>
  - CLAUDE.md: <absent | present | scaffold-managed | hand-edited>
- existing_catalog: <absent | present | subdir-only | root-present>
- expected_changed_paths:
  - <repo-relative path>
- preserve_paths:
  - <repo-relative path>
- risks:
  - <risk>
- verification_plan:
  - `<command>`
- next_action: <dispatch implementation child | stop>
```

### Adoption implementation done

```markdown
## Implementation Done

- changed_paths:
  - <repo-relative path>
- preset_mode: central | repo-local
- scaffold_status: <clean | drift explained>
- docs_status: <passed | not applicable | failed>
- commit_hash: <origin-reachable hash or local-only observation>
- callback_required: true
- residual_risks:
  - <risk or none>
```

### Process correction

```markdown
## Process correction

- observed_gap: <missing decision record | callback missing | local-only commit | other>
- recovered_from: #<issue> j#<journal>
- impact: <none | review anchor changed | close blocked>
- correction:
  - <what was recorded or re-routed>
- invariant_status:
  - owner_approval: preserved
  - close_gate: preserved
  - release_gate: preserved
```

## 参照正本

- `vibes/docs/logics/scaffold-rules.md`
- `vibes/docs/logics/bootstrap.md`
- `vibes/docs/logics/coordinator-sublane-development-flow.md`
- `vibes/docs/logics/cross-project-cockpit-smoke-runbook.md`
- `vibes/docs/logics/executive-terminal-setup.md`
- `vibes/docs/logics/workspace-registry.md`
- `.mozyo-bridge/rules/docs_catalog_governance.yaml`
- `vibes/docs/rules/public-private-boundary.md`
- `skills/mozyo-bridge-agent/references/workflow.md`

## 検証

この runbook または catalog registration を変更したら、少なくとも次を実行する。

```bash
mozyo-bridge docs resolve vibes/docs/logics/existing-project-sublane-adoption.md .mozyo-bridge/docs/catalog.yaml
mozyo-bridge docs validate --repo .
mozyo-bridge docs validate --check-file-coverage --repo .
mozyo-bridge docs generate-file-conventions --check --repo .
mozyo-bridge docs audit-impact --all-changed --check-generated --repo .
git diff --check
```
