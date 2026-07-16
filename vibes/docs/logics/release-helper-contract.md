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

- `release publish --testpypi --source-sha <40-hex> --expected-version <X.Y.Z> --source-ref <origin 上の ref literal>` — exact-candidate 内部 beta dispatch (Redmine #13601)。`gh workflow run testpypi.yml --ref main -f source_sha=... -f expected_version=... -f source_ref=... -f dispatch_nonce=...` を構成する。workflow の定義 / event ref は `main` 固定 (artifact authority は SHA、workflow authority は main)。3 input は必須で、`source_sha` は exact 40-hex に validate、`expected_version` は PEP 440 shape に validate、`source_ref` は origin 上で action-time に `source_sha` へ解決すべき approved ref であり、**origin 上の ref literal として綴る** (`### source_ref Spelling Policy`)。`--version` は `--expected-version` の後方互換 alias。dispatch は run-name 中の unique `dispatch_nonce` で決定的に相関し (latest-one 推測禁止)、exact 1 件以外は fail-closed で `result: blocker` を返す。相関した run-id を active ticket に貼れる shape で stdout に出す。fail-closed 照合そのもの (HEAD==SHA / version mirror / candidate test.yml が trusted origin/main と byte 一致 / Test CI success / version 未使用 schema fail-closed / source_ref が exact 1 件の named origin ref。#13601 j#76006 F1-F3) は trusted な main-defined workflow build job の inline gate 内で行い、**gate authority は workflow 側に残す**。helper が client 側で行うのは、`source_ref` の綴り検査と origin 解決の **preflight** (`### Action-Time Client Preflight`)、dispatch、nonce 相関のみである。preflight は workflow gate の複製ではなく mirror であり、workflow 側 gate を削ってよい根拠にはならない (untrusted client の検査結果を trusted gate の代わりにしない)。workflow 完了 polling は `release workflow wait` に委ねる。
- `release publish --pypi --tag vX.Y.Z` — production publish の trigger を組み立てる。具体的には `gh release create vX.Y.Z --verify-tag --title "vX.Y.Z" --notes-file <path>` のドライランを出力し、release notes file path と tag が揃っていることを assert する。`--execute` flag が明示的に渡された場合のみ `gh release create` を実行する。
- `release publish --plan` — TestPyPI / PyPI それぞれで、現在の git ref / pyproject version / 最新の `Test` workflow conclusion / TestPyPI 既存 version の有無を読み取り、operator が次に取りうる選択肢を列挙する。判定はしない。

#### `source_ref` Spelling Policy (Redmine #13883)

`source_ref` は **remote 上の ref literal** である。helper も workflow も、この値を `git ls-remote origin <source_ref>` へそのまま渡して解決する。したがって「git が local で表示する名前」ではなく「**origin がその ref を綴っている名前**」を渡す。両者は別物であり、本 doc / help / examples 全体でこの区別を維持する。

| 種別 | 例 | 実体 | `source_ref` として |
| --- | --- | --- | --- |
| remote ref literal (canonical) | `refs/heads/int_release_x` | origin 上に存在する ref の完全 path | **推奨** (最も曖昧さが少ない)。ただし exactly-one は保証されない (下記) |
| remote ref literal (短縮) | `int_release_x` | ls-remote が tail 一致で解決 | 受理。exactly-one は保証されない (下記) |
| local remote-tracking name | `origin/int_release_x` | local の `refs/remotes/origin/int_release_x` の表示名。ただし remote 上に同名 branch が存在しうる | **reject** (曖昧なため) |
| local remote-tracking full path | `refs/remotes/origin/int_release_x` | remote ref の local mirror。remote は branch を `refs/heads/` に publish する | **reject** |

**採用 policy: reject with exact correction (単一 policy)。** local remote-tracking 表記を受けたら、helper は dispatch 前に停止し、そのまま貼れる訂正を添えて exit する。**silent normalize (`origin/<branch>` -> `<branch>`) は採用しない。**

却下理由 (`origin/<branch>` の normalize):

- **`origin/<branch>` は曖昧である (これが reject の理由)。** remote は `origin/<branch>` という名前の branch を実際に持てる (`refs/heads/origin/<branch>`)。よって `origin/main` は「local が表示する `main`」とも「remote 上の literal な `origin/main`」とも読める。`main` へ書き換えると、artifact authority を **別 commit へ silent に付け替えうる**。regression test `test_origin_prefixed_branch_can_really_exist_on_origin` が実 git に対してこの反例を pin している。
  - **注意 (Redmine #13883 j#79995 F3)**: reject の理由は「zero 解決するから」**ではない**。zero 解決は同名 branch が無い場合の典型的な結末にすぎず、上記のとおり 1 件解決することもある。診断 message / docs / help は「曖昧だから拒否する」を理由として書き、「常に zero」と断定しない。
- **normalize 先自体が exactly-one とは限らない。** `git ls-remote <pattern>` は exact lookup ではなく **ref 名の tail 一致 glob** である。`main` は `refs/heads/main` に加え `refs/tags/main` や `refs/heads/origin/main` にも一致する。「安全に normalize できる」前提が成立しない。
- **周辺 gate との整合。** この経路は他のすべての link (exact 40-hex SHA、exactly-one ref、candidate `test.yml` の byte 一致) で曖昧さを拒否している。helper だけが operator の意図を推測すると、そこが唯一の soft spot になる。
- **reject の代償が小さい。** preflight が origin を引く以上、helper は正しい綴りを exact に提示できる。operator は 1 回貼り直すだけで、silent な取り違えは起きない。

`origin/<branch>` という名前の branch を **literal に指したい** 場合の逃げ道は残す: `--source-ref refs/heads/origin/<branch>` を渡す。reject message がこの exact form を併記する。

#### Action-Time Client Preflight (Redmine #13883)

`release publish --testpypi` は `gh workflow run` に到達する **前に**、client 側で origin を引いて次を確定する。これは trusted workflow の gate `Verify source_ref resolves to source SHA on origin` の **client mirror** であり、置き換えではない (workflow 側の inline gate は trusted authority として残す)。

- `git ls-remote origin <source_ref>` の結果から peel 行 (`^{}`) を落とし、**non-peel でちょうど 1 件** を要求する。
- その 1 件の tip が `--source-sha` と一致することを要求し、`source_ref_resolved: <ref> -> <sha>` として stdout に出す。
- **zero / multi / mismatch のいずれでも dispatch を 0 回**にする (`gh workflow run` を呼ばない)。charset (`[A-Za-z0-9._/-]+`、glob / refspec metachar / whitespace 拒否) は workflow gate と同一で、shell-safety guard も兼ねる。

peel 行を落とす帰結として、**annotated tag の non-peel tip は tag object であって commit ではない**。よって annotated tag を `source_ref` に渡すと mismatch として refuse される。これは server 側 gate と同じ挙動であり、client が server より緩くならないための意図的な parity である。

#### exactly-one は構造保証ではなく動的検査 (Redmine #13883 j#79995 F1)

`ls-remote` の tail 一致は **full path 入力にも作用する**。remote に branch `foo/refs/heads/main` があると、`git ls-remote origin refs/heads/main` は `refs/heads/foo/refs/heads/main` と `refs/heads/main` の **2 件**を返す (isolated real-git で実証、regression test に pin)。したがって:

- **どの綴りも「構造上つねに exactly-one」ではない。** `refs/heads/<branch>` は *最も曖昧さが少ない canonical form* であって、一意性が保証された form ではない。docs / help / 診断 message でこれを「always exactly one」と書かない。
- exactly-one は **preflight と server gate が動的に検査する不変条件**である。衝突時は client / server の双方が同じ logic で refuse するため、fail-closed と parity は保たれる。
- **回復手順は 2 通りあり、`ls-remote` の match facts で判定する** (入力の見た目で判定しない):
  - **入力が一致した ref のいずれとも完全一致しない** -> 入力は tail pattern であり、より特定的な綴りが存在する。列挙した ref の full path を verbatim で渡せば解消しうる。
  - **入力が一致した ref のいずれかと完全一致する** -> 入力は既にその ref の完全名であり、再提示では絞れない。回復は (a) origin 側の衝突 ref を rename / 削除する、(b) 一意に解決する別 ref を使う、のいずれか。
  - **`refs/` prefix を「full path である」の判定に使わない** (Redmine #13883 j#80048 R2-F1)。branch 短縮名は合法に `refs/` で始まれる (`refs/foo` は origin 上で `refs/heads/refs/foo` になる)。prefix で判定すると、この入力を full path と誤分類し、**実際に有効な full-path 訂正を隠して不要な origin 側 ref 削除を勧める**。判定は「入力と完全一致する ref が解決結果にあるか」で行う。

client 側で full path の exact-match semantics を実装して衝突を回避する案は **採らない**。server が glob のままなので、client だけ exact-match にすると **client が greenlight した ref を server が refuse する**状態が生じ、本 contract が掲げる「client は server より緩くならない」parity を破る。exact-match を入れるなら trusted workflow gate 側と同時に変更する必要があり、それは別 issue の scope である。

これは #13601 の trust model を弱めない。preflight は read-only な `git ls-remote` のみで、**判断を自動化しない** (曖昧なら止めるだけで、ref を選び直したり推測したりしない)。狙いは、build 前に落ちると分かっている dispatch を発行しないこと。run `29481593519` はこの preflight が無かったために起動後 build 前に失敗した。

`release publish --pypi` は GitHub Release の published event を発火させる権利を helper に渡すという意味で危険度が高い。default は dry-run、`--execute` を明示しない限り `gh release create` を実行しない。helper は GA / beta の判断を内蔵しない (後述の Boundary を参照)。

### `release workflow` (polling / summary)

`release check workflow` だけでは表現しづらい、run の集約 / 結果 summary 用に必要な範囲だけを admit する。speculative な future helper は admit しない。

- `release workflow runs --workflow <name>` — 指定 workflow の最近 run を `created_at` / `status` / `conclusion` / `head_sha` / `html_url` で一覧する。
- `release workflow wait --run-id <id> --timeout <seconds>` — 指定 run が `completed` になるまで polling し、最終 `conclusion` を返す。`--timeout` を超えたら non-zero で終了する。何の judgment もしない。

この family は GitHub Actions の状態を active ticket に貼り直すためのもので、release 全体を helper の内部 state machine で進めるためのものではない。

## Human / Script / Ticket Boundary

helper が触ってよい層と触ってはいけない層を明示する。

### Helper (script) が自動でやってよいこと

- read-only な検査 (`git status` / `git log -S` / `git grep` / `git remote` / `git ls-remote` / build artifact 展開後の grep / GitHub Actions run の status 取得)。
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
- `release publish --testpypi` は同一 version の 2 回目以降 dispatch を helper 側で blocker にしない (TestPyPI 側で reject されるため state は GitHub 側で管理される)。version の一意性判断は helper に持たせない。ただし `source_ref` が origin 上で exactly-one に解決し `source_sha` と一致することは dispatch 前に client preflight で確認し、満たさなければ dispatch を発行しない (`### Action-Time Client Preflight`)。これは judgment の自動化ではなく、確実に build 前に失敗する dispatch を出さないための機械的検査である。
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
| TestPyPI publish workflow の dispatch | `release publish --testpypi --source-sha <40-hex> --expected-version <X.Y.Z> --source-ref <origin 上の ref literal>` | main-fixed exact-candidate workflow dispatch + `source_ref` の action-time preflight (zero/multi/mismatch は dispatch 0 回, Redmine #13883) + nonce 相関 (Redmine #13601) |
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
