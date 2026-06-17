# Release Helper Subcommand Contract

## Scope

この doc は `mozyo-bridge` の release 操作のうち、機械化可能な mechanics を `mozyo-bridge release <subcommand>` 系の stepwise helper として括り出すための contract を定義する。実行手順そのものや、release Gate の judgment 規約は引き続き `vibes/docs/logics/release-flow.md` と `vibes/docs/rules/release-distribution.md` を正本とする。

この contract は release を 1 つの opaque な `release do-everything` script に collapse するためのものではない。helper はあくまで release-flow の各 step を再現性のある CLI として薄く包むもので、active ticket による durable workflow (Redmine journal / Asana comment; preset に従う) と human release judgment を上書きしない。

## Helper Command Families

すべて `mozyo-bridge release <family> [<subcommand>] [--option ...]` の形を取る。command family は 4 つに固定し、speculative な future family を増やさない。

### `release check`

local guardrail と artifact / workflow 状態を検査するための read-only helper 群。worktree や remote state を mutate しない。

- `release check tree` — `release-flow.md` の `Source Tree Hygiene` (`git status --short --branch` / `git log -S'/Users/'` / `git grep` で個人ホーム絶対パス・secret-shape candidate を探し、credential candidate を classifier に通す検査) を 1 command として再現する。release blocker を検出したら non-zero で終了する。
- `release check scaffold` — `release-flow.md` の `Fresh Scaffold Smoke` (isolated home / isolated target で全 preset の fresh scaffold + `scaffold status`) を 1 command として再現する。生成物に host 固有パスが含まれていないことと、portable `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}` 表現が残ることを assert する。
- `release check artifact` — `release-flow.md` の `Build Artifact Inspection` (`python -m build` 結果の wheel / sdist を展開し、`/Users/` 等の personal path と secret-shape candidate を scan し、credential candidate を classifier に通す) を 1 command として再現する。dist artifact path と classifier 後に残った blocker を stdout に列挙し、blocker disposition は人間に残す。
- `release check drift` — `release-flow.md` の `Canonical Renderer / Plugin Mirror Drift` を 1 command として再現する。`mozyo-bridge scaffold canonical --check` (router pair + governed workflow pair, Redmine #10345 / #10426) と `scripts/sync_plugin_skill.sh --check` (plugin mirror, Redmine #10663) を順に subprocess として実行し、いずれかが drift を検出したら strict-fail (exit 1) で `result: blocker` を返す。helper 自身は worktree を mutate せず、復旧 command (`mozyo-bridge scaffold canonical` / `scripts/sync_plugin_skill.sh`, いずれも `--check` なし) を stdout に verbatim で列挙する。判断 (例: drift を accept して release を進める) は operator に残す。
- `release check workflow --run-id <id>` — GitHub Actions `Test` / `Publish to TestPyPI` / `Publish to PyPI` の run status / conclusion を取得し、`success` / `failure` / `in_progress` を出力する。judgment (例: 「`failure` でも release を進める」) は実行しない。

`release check` 全体としての挙動は read-only / idempotent / dry-run-by-default で固定する。`--fix` / `--auto-correct` 系の flag は導入しない。

### `release bump`

repo の **現在 authoritative な release-version mirror set** を 1 つの version 文字列に揃える単一目的の helper。 mirror set を超えた file を変更してはならない。

現時点 (本 contract が land する `mozyo-bridge 0.2.0` 系列) の release-version mirror set は以下の 2 file に固定する。これは durable な repo invariant であり、`tests/test_mozyo_bridge.py:test_module_version_matches_pyproject_version` がこの一致を test として enforce している。bump-task audit `1214798011715593` でこの 2-file mirror pattern が現行 release の正しい姿として accept 済み。

- `pyproject.toml` の `[project].version`
- `src/mozyo_bridge/__init__.py` の `__version__`

将来 mirror set が増減した場合は、本 contract と `release-flow.md` を同時に update してから helper の implementation が追従する。helper の内部に「mirror set はこの 2 file である」をハードコードせず、release-flow doc が明示的に列挙する set を helper が読み取って follow する形にする。それでも helper が触ってよい file は contract と release-flow doc がその時点で列挙している mirror set のみであり、それ以外は触らない。

`release-flow.md` の wording sync (現状の単一-file 風の説明から 2-file mirror への揃え) は follow-up task `1214798255863579` で進行中であり、本 contract はその follow-up を `mirror set はこの contract 上で 2-file である` と確定させるための durable record でもある。

- `release bump --to <version>` — 現在 authoritative な mirror set 全 file (`pyproject.toml` + `src/mozyo_bridge/__init__.py`) を指定 version に書き換える。書き換え後の worktree を `git status` / `git diff` で表示する。commit / push / tag は実行しない。書き換え対象が事前 grep / parser で見つからない (= mirror set が当時の repo と乖離している) ときは strict fail とし、operator に release-flow doc 側との sync 漏れを通知する。
- `release bump --check` — 現在の mirror set 各 file の version 値、`git log` 上の前回 release commit、tag (`git tag --list 'v*'`) を併記して dry-run で出力する。mirror set 内に値の不一致 (例: `pyproject.toml` だけ進んでいる) がある場合は exit non-zero とし、`test_module_version_matches_pyproject_version` 相当の invariant 違反を表面化する。

判断 (例: pre-release で `a2` に進めるか beta `b1` に出すか) と `git commit -m "Release vX.Y.Z"` / `git push` は operator が実行する。helper は commit / push / tag をしない。これは `release-flow.md` の `code 変更と version bump を同じ commit に混ぜない。bump は単独 commit にする` を helper 側で先回りに enforce しないため (operator の commit shape をそのまま尊重するため) に必要。helper が複数 file を書き換える点は invariant の維持であって、judgment の自動化ではない。

### `release publish`

publish workflow の dispatch / 状態確認に専念する helper。version 文字列の整合・gate 判定・release notes は扱わない。

- `release publish --testpypi --version <X.Y.Z>` — `gh workflow run testpypi.yml --ref main -f version=X.Y.Z` 相当の dispatch を実行する。dispatch 後の run-id を active ticket に貼れる shape で stdout に出す。workflow 完了の polling はこの subcommand では行わず、`release check workflow --run-id <id>` に明示的に委ねる。
- `release publish --pypi --tag vX.Y.Z` — production publish の trigger を組み立てる。具体的には `gh release create vX.Y.Z --verify-tag --title "vX.Y.Z" --notes-file <path>` のドライランを出力し、release notes file path と tag が揃っていることを assert する。`--execute` flag が明示的に渡された場合のみ `gh release create` を実行する。
- `release publish --plan` — TestPyPI / PyPI それぞれで、現在の git ref / pyproject version / 最新の `Test` workflow conclusion / TestPyPI 既存 version の有無を読み取り、operator が次に取りうる選択肢を列挙する。判定はしない。

`release publish --pypi` は GitHub Release の published event を発火させる権利を helper に渡すという意味で危険度が高い。default は dry-run、`--execute` を明示しない限り `gh release create` を実行しない。helper は GA / beta の判断を内蔵しない (後述の Boundary を参照)。

### `release workflow` (polling / summary)

`release check workflow` だけでは表現しづらい、run の集約 / 結果 summary 用に必要な範囲だけを admit する。speculative な future helper は admit しない。

- `release workflow runs --workflow <name>` — 指定 workflow の最近 run を `created_at` / `status` / `conclusion` / `head_sha` / `html_url` で一覧する。
- `release workflow wait --run-id <id> --timeout <seconds>` — 指定 run が `completed` になるまで polling し、最終 `conclusion` を返す。`--timeout` を超えたら non-zero で終了する。何の judgment もしない。

この family は GitHub Actions の状態を active ticket に貼り直すためのもので、release 全体を helper の内部 state machine で進めるためのものではない。

## Human / Script / Ticket Boundary

helper が触ってよい層と触ってはいけない層を明示する。

### Helper (script) が自動でやってよいこと

- read-only な検査 (`git status` / `git log -S` / `git grep` / build artifact 展開後の grep / GitHub Actions run の status 取得)。
- repo の authoritative な release-version mirror set 全 file (現状: `pyproject.toml` + `src/mozyo_bridge/__init__.py`) の version 文字列の機械的書き換え (`release bump --to <version>`)。worktree に diff として残し、commit はしない。mirror set 自体の定義は本 contract と `release-flow.md` が正本。
- GitHub Actions workflow の dispatch (`gh workflow run`)。
- GitHub Actions workflow の polling / status 取得。
- production publish 系コマンドの dry-run 出力。

### Operator (human) が引き続き owner であること

- pre-release を続けるか、beta tag を切るか、GA に進むかの **GA/beta judgment**。helper はこの判定を内部 state に持たない。
- release notes の最終文言。helper は path を assert するだけで、生成も書き換えもしない。
- `git commit` / `git push` / `git tag -a` / `gh release create --execute` 系の **state-mutating release action** の最終 trigger。
- workflow が `failure` で返ったとき、`release を中断するか / 再 run するか / blocker を許容するか` の判定。
- `release check tree` / `release check artifact` で classifier 後の blocker が残ったときに、clean に直すか設計 disposition として受領するかの判定。

### Active ticket が引き続き正本であること

- active release ticket の description / journal / comment が **durable source of truth** である。Redmine governed preset では Redmine issue / journal、Asana governed preset では Asana task / comment を使う。helper の stdout や local log を durable record と見做さない。
- 各 helper の attempted command と observed result は active ticket に operator が貼る。helper は ticket system には書き込まない。
- residual blocker / 受領 method / commit hash / workflow run url の記録先は active ticket であり、helper-local の `.mozyo-bridge/` 配下 state ではない。
- audit-owned commit hash の記録 (`Audit: Redmine journal <journal_id>` / `Audit: Asana comment <comment_id>` 等) は helper 化対象に含めない。Audit-Owned Commit Authority の経路を経た human / audit actor が直接 active ticket に書く。

## Failure Posture

### Strict fail vs warning-only

- `release check tree` / `release check scaffold` / `release check artifact` は personal path や classifier 後に残る real credential literal の検出を strict fail とする。release blocker をそのまま exit code 0 にしない。
- `release check workflow` / `release workflow wait` の `conclusion == failure` は strict fail ではなく **observed failure を non-zero exit で返すだけ**。 `この failure を receive するかどうか` は operator の judgment に残す (helper は再 run しない、tag を巻き戻さない)。
- `release check tree` / `release check artifact` の credential-shape candidate は second-stage classifier に通し、env read / type annotation / identifier reference / placeholder / test sentinel など safe-code pattern は helper が除外する。placeholder ではない literal credential value は blocker であり、token punctuation (`.`, `/`, `+`, `=`, `_`, `-`) を含む値も blocker として扱う。classifier 後に blocker が残った場合は strict fail で止め、operator が clean に直すか active ticket に disposition と判断理由を残してから再実行する運用を helper 側でも担保する。

### Never mutate implicitly

- `release publish --pypi` は default で dry-run。 `--execute` を明示しない限り `gh release create` を呼ばない。
- `release bump` は authoritative な mirror set 全 file の worktree に diff を残すのみで、`git add` / `git commit` / `git push` をしない。mirror set を超えた file (例: docs / src / scaffold preset) には絶対に触らない。
- helper は `git tag` を作成しない。tag は operator の `git tag -a vX.Y.Z` で残す。
- helper は TestPyPI / PyPI 上の既存 artifact を取り下げない。`Rollback` 系の操作は helper の admit する surface から除外する (release-flow.md の Rollback 節を operator が手で実行する)。

### Dry-run / idempotent / resumable

- すべての `release check` は read-only / idempotent。連続実行で同じ output を返すことが期待値。
- `release publish --testpypi` は同一 version の 2 回目以降 dispatch を helper 側で blocker にしない (TestPyPI 側で reject されるため state は GitHub 側で管理される)。helper は dispatch attempt を発行するだけ。
- `release workflow wait` は同じ run-id に対して resumable。途中で中断しても再度 wait をかけ直せる。
- `release bump --to <version>` は冪等 (現在 version と同じ値を渡された場合は no-op として明示する)。

## Mapping to `release-flow.md`

`release-flow.md` の既存 step との対応関係を明示する。helper は新しい release process を作らず、既存 release flow の mechanical step を CLI に括り出すだけである。

| `release-flow.md` の step | helper subcommand | 自動化レベル |
| --- | --- | --- |
| 通常確認 (`unittest discover` / `pip wheel` / `--help`) | (helper 化しない) | operator が手で実行する |
| tmux / delivery 変更時の `smoke/real_tmux_notify_smoke.py` | (helper 化しない) | tmux 環境依存のため operator が実行 |
| `Source Tree Hygiene` (git status / git log / git grep) | `release check tree` | strict-fail 検査として実行 |
| `Fresh Scaffold Smoke` | `release check scaffold` | strict-fail 検査として実行 |
| `Build Artifact Inspection` (`python -m build` + grep) | `release check artifact` | strict-fail 検査として実行 |
| `Canonical Renderer / Plugin Mirror Drift` | `release check drift` | strict-fail 検査として実行 |
| `Release Ref Consistency` (pyproject version / tag / install ref) | `release bump --check` + `release check workflow` | read-only の参照情報を表示。mirror set 内 version 不一致は exit non-zero |
| `Version Bump` (release-version mirror set の書き換え + 単独 commit) | `release bump --to <version>` | mirror set 全 file (現状: `pyproject.toml` + `src/mozyo_bridge/__init__.py`) を worktree に書き換えるだけ |
| TestPyPI publish workflow の dispatch | `release publish --testpypi --version <X.Y.Z>` | workflow dispatch のみ |
| `Test` / `Publish to TestPyPI` / `Publish to PyPI` workflow の green 確認 | `release check workflow` / `release workflow wait` | run status / conclusion 取得のみ |
| Fresh install smoke (`pipx install ...`) | (helper 化しない) | tester 環境依存のため operator が実行 |
| GitHub Release 作成 (`gh release create`) | `release publish --pypi --tag vX.Y.Z` | default dry-run / `--execute` で実行 |
| `git push --delete origin vX.Y.Z` / `git tag -d` (Rollback) | (helper 化しない) | rollback は operator が手で実行 |

ここに表れない release-flow.md の節 (Internal Beta Gate / Production Release Gate / Version Policy など) はすべて judgment の節であり、helper の自動化対象に含まれない。

## Hard Constraints (再掲)

- 全 release を 1 command に collapse する `release run` / `release all` 系 helper は admit しない。
- GA / beta の判断、release notes 文言、blocker 受領可否を helper の内部判定にしない。
- active ticket driven な durable workflow を helper-local state (`.mozyo-bridge/release-state.json` 等) で置き換えない。
- 現在 release line がまだ exercise していない future helper (例: `release announce`, `release notify-slack`, `release rollback`, `release sign`) は admit しない。必要が出た時点で別 active ticket として contract を拡張する。

## Followup Implication

この contract が確定した後、以下の subtask の wording を見直す:

- `1214798597691736` *Implement local release-check / artifact / workflow polling helpers* — `release check tree` / `release check scaffold` / `release check artifact` / `release check workflow` / `release workflow runs|wait` の実装範囲をこの contract に合わせる。dispatch / mutate を含まないことを明示する。
- `1214798644479548` *Implement version-bump and publish helpers for TestPyPI / PyPI flows* — `release bump` は worktree への書き換えだけに留めること、mirror set 全 file (現状: `pyproject.toml` + `src/mozyo_bridge/__init__.py`) を 1 invocation で書き換え、`tests/test_mozyo_bridge.py:test_module_version_matches_pyproject_version` の invariant を実装側でも前提にすること、`release publish --pypi` の default dry-run / `--execute` 規約、`gh release create` を helper が握る範囲を明示する。
- `1214798360431566` *Update release-flow docs and operator guidance for helper-driven release execution* — `release-flow.md` の各 step に helper subcommand を併記する update を行う。judgment 節を helper 化しないことも明示する。 release-version mirror set が現状 2-file (`pyproject.toml` + `src/mozyo_bridge/__init__.py`) であることを `release-flow.md` 側で明示し、follow-up `1214798255863579` の wording-sync を本 contract と整合させる。

これらの subtask は本 contract が定める surface と boundary を逸脱しない範囲で実装する。判断付きの自動化が必要な気配が出た時点で、subtask の中で済ませず、active ticket として contract 側に戻して合意し直す。
