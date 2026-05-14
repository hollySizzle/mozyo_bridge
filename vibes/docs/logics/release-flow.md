# Release And Verification Logic

## Helper-Driven Execution Overview

`mozyo-bridge release …` helper family は本 doc に記述された mechanical step を CLI として再現する read-only / bounded-mutation な薄い wrapper である。helper の admit する surface と human-vs-script boundary は `vibes/docs/logics/release-helper-contract.md` を正本とする。本 doc は判断付きの release step (GA / beta judgment, release notes wording, blocker disposition) を helper 化しない。

helper invocation を以下の節で各 step に併記する。helper を呼ばずに manual command を直接実行する従来手順も引き続き有効であり、helper はそれと等価な mechanical step を 1 command に括り出すための facade である。Asana task / comment が引き続き durable な作業ログであり、helper stdout は durable record の代替にならない。

| step | helper subcommand | judgment 残し方 |
| --- | --- | --- |
| Source Tree Hygiene | `mozyo-bridge release check tree` | false-positive 判定は operator |
| Fresh Scaffold Smoke | `mozyo-bridge release check scaffold` | strict-fail; preset 修正で再実行 |
| Build Artifact Inspection | `mozyo-bridge release check artifact` | false-positive 判定は operator |
| Release Ref Consistency (mirror set 内 version 確認) | `mozyo-bridge release bump --check` | mirror set 不一致は strict-fail |
| GitHub Actions run status / conclusion 確認 | `mozyo-bridge release check workflow --run-id <id>` | `failure` 受領可否は operator |
| GitHub Actions run polling | `mozyo-bridge release workflow wait --run-id <id> --timeout <s>` | timeout exit (124) は operator が次手を決定 |
| Version Bump (mirror set 全 file の version 書き換え) | `mozyo-bridge release bump --to X.Y.Z` | `git commit` / `git push` / `git tag` は operator |
| TestPyPI publish workflow dispatch | `mozyo-bridge release publish --testpypi --version X.Y.Z` | dispatch 後の判定は operator |
| GitHub Release 作成 (production publish trigger) | `mozyo-bridge release publish --pypi --tag vX.Y.Z --notes-file <path>` | default dry-run; `--execute` を明示しない限り `gh release create` を呼ばない |
| Release 状況の at-a-glance 一覧 | `mozyo-bridge release publish --plan` | 判定はしない; operator の選択肢を列挙 |

## 通常確認

変更内容に応じて、必要最小限の verification を選ぶ。

```bash
python -m unittest discover -s tests -v
python -m pip wheel . --no-deps -w /tmp/mozyo_bridge_dist
python -m mozyo_bridge --help
```

local test は project の対応 Python version と同じ環境で実行する。

## tmux / delivery 変更時

tmux delivery、pane resolution、marker safety、CLI notification contract を変更した場合は real smoke test を実行する。

```bash
python smoke/real_tmux_notify_smoke.py
MOZYO_BRIDGE_COMMAND=mozyo-bridge-testpypi python smoke/real_tmux_notify_smoke.py
```

`smoke/real_tmux_notify_smoke.py` は strict `--mode standard` rail のみを自動検証する。relaxed `--mode queue-enter` rail (Asana `1214782240916053` 配下の v0.2 contract、v0.3 で deterministic preflight、v0.3.1 で `node` を両 receiver の weak identity に再分類) は real Claude / Codex TUI 上の prompt queue 挙動と pane metadata に依存するため、同 smoke では模擬しない。queue-enter rail を触る変更は同 smoke header の docstring に記載した手順 (`mozyo-bridge handoff send --mode queue-enter` を marker 観測あり / 観測なし / strict regression の 3 ケース、および v0.3 preflight spot-check 3 ケース (foreign-session / inactive-split / non-agent reject) で実機確認) を Asana task に記録する。

## Release Flow

1. Asana release task から開始する。
2. local unit test と build check を実行する。
3. Release Artifact Guardrails を実行する。
4. `main` に push し、GitHub Actions `Test` の成功を確認する。
5. TestPyPI は `Publish to TestPyPI` workflow を使う。
6. TestPyPI install を `pipx` で検証する。
7. 社内ベータ配布は TestPyPI install 検証で完了とする。
8. production PyPI 公開は、別途 production release として明示判断する。

## Release Artifact Guardrails

Release 前に、git tree、scaffold output、build artifact の三つを検査する。`mozyo-bridge --version`
だけでは release commit の一致や scaffold preset の中身を判定できないため、version 文字列を唯一の根拠にしない。

### Source Tree Hygiene

以下は release commit 直前に必ず確認する。secret ではない個人ホーム絶対パスでも、public repo では release blocker として扱う。

```bash
git status --short --branch
git log --all -S'/Users/' -- AGENTS.md CLAUDE.md src skills vibes README.md pyproject.toml
git grep -nE '/Users/|/home/[^/]+/|C:\\Users\\|\\.env|pypirc|token|secret|password' -- \
  ':!*.pyc' ':!build' ':!dist' ':!.git' ':!.venv' ':!tmp'
```

`git grep` は false positive を含み得る。false positive は Asana release task に理由を記録する。
`/Users/<name>` のような個人ホーム絶対パスが router、skill、docs、scaffold preset、manifest に入っている場合は release しない。

Helper:

```bash
mozyo-bridge release check tree
```

上記の `git status` / `git log -S'/Users/'` / `git grep` を 1 command として再現する。同じ pathspec exclusion を内側で適用し、personal path / secret-shape の検出を strict-fail (exit 1) で返す。false-positive 判定 (例: docs 内の意図された path) は引き続き operator が Asana task に残してから release を進める。

### Fresh Scaffold Smoke

local checkout から、isolated home / isolated target に対して全 preset を fresh scaffold する。
生成物に host 固有パスが入らず、portable rule path と `scaffold status` が揃うことを確認する。

```bash
tmp="$(mktemp -d)"
home="$tmp/home"
python -m mozyo_bridge rules install --home "$home"
for preset in asana redmine none; do
  project="$tmp/project-$preset"
  mkdir -p "$project"
  python -m mozyo_bridge scaffold rules "$preset" --target "$project" --home "$home"
  rg -n '/Users/|/home/[^/]+/|C:\\Users\\' \
    "$project/AGENTS.md" "$project/CLAUDE.md" "$project/.mozyo-bridge/scaffold.json" && exit 1
  rg -n --fixed-strings \
    "\${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/$preset/agent-workflow.md" \
    "$project/AGENTS.md" "$project/CLAUDE.md" "$project/.mozyo-bridge/scaffold.json"
  python -m mozyo_bridge scaffold status --target "$project" --home "$home" | rg 'clean'
done
```

Helper:

```bash
mozyo-bridge release check scaffold
```

上記 loop を 1 command で再現する。helper は tmp home / tmp target を都度新規に確保し、全 preset (`asana` / `redmine` / `none`) について host-path leak / portable rule path / `scaffold status: clean` を検査する。tmp 領域は helper 完了時に自動で破棄され、repo の worktree や `~/.mozyo_bridge` は触らない。

### Build Artifact Inspection

Release artifact そのものを展開して検査する。sdist は root docs を含む場合があるため、wheel だけで判定しない。

```bash
rm -rf dist
python -m build
tmp="$(mktemp -d)"
for artifact in dist/*; do
  case "$artifact" in
    *.whl) python -m zipfile -e "$artifact" "$tmp/wheel" ;;
    *.tar.gz) mkdir -p "$tmp/sdist"; tar -xzf "$artifact" -C "$tmp/sdist" ;;
  esac
done
rg -n '/Users/|/home/[^/]+/|C:\\Users\\|pypirc|token|secret|password' "$tmp" && exit 1
```

この検査で false positive が出た場合も、release task に artifact path と判断理由を残す。

Helper:

```bash
mozyo-bridge release check artifact
```

helper は `release check` family の read-only invariant を守るため repo の `dist/` を一切触らない。`python -m build --outdir <tmp>/dist` で隔離 tmp に書き出し、wheel / sdist を `<tmp>/extracted` に展開してから grep する。false-positive 判定は引き続き operator が Asana task に残し、helper は strict-fail (exit 1) を返す。

### Release Ref Consistency

docs / command / scaffold preset / skill archive が同じ release commit を指すことを確認する。

- release-version mirror set (現状 2 file: `pyproject.toml` の `[project].version` + `src/mozyo_bridge/__init__.py` の `__version__`) は同じ version 文字列を保つ。bump は単独 commit にする。authoritative な mirror set 定義は `vibes/docs/logics/release-helper-contract.md` の `release bump` 節を正本とする。
- `mozyo-bridge --version` は package version だけを示す。GitHub `main` と PyPI artifact が同じ version string を持つ場合があるため、差分確認には使わない。
- TestPyPI / PyPI の fresh install smoke は、package version と同じ git ref から install scripts / skill tree を取得する。tag release なら `MOZYO_BRIDGE_SKILL_REF=vX.Y.Z` を使う。
- tag は release commit を指す annotated tag として作成し、tag 作成後に `git ls-remote origin refs/tags/vX.Y.Z` で remote ref を確認する。

Helper:

```bash
mozyo-bridge release bump --check
```

mirror set 全 file の現在 version 値、`v*` tag 一覧、最新 `Release vX.Y.Z` commit を併記して dry-run で出力する。mirror set 内の値が不一致 (例: `pyproject.toml` だけ進んでいる) のときは exit non-zero とし、`tests/test_mozyo_bridge.py:test_module_version_matches_pyproject_version` 相当の invariant 違反を CLI 層で再現する。

```bash
mozyo-bridge release check workflow --run-id <id>
mozyo-bridge release workflow runs --workflow <name>
mozyo-bridge release workflow wait --run-id <id> --timeout <seconds>
```

`Test` / `Publish to TestPyPI` / `Publish to PyPI` workflow の run status / conclusion を取得する read-only helper。helper は judgment しない (`failure` を受領するかどうかは operator)。

## Fresh Install Smoke

TestPyPI / PyPI の release acceptance では、local checkout / local wheel / editable install を代用しない。

TestPyPI:

```bash
pipx install --force --backend pip \
  --index-url https://test.pypi.org/simple/ \
  --pip-args "--extra-index-url https://pypi.org/simple/" \
  mozyo-bridge==X.Y.Z
```

PyPI:

```bash
pipx install --force mozyo-bridge==X.Y.Z
```

Fresh install 後の minimum smoke:

```bash
mozyo-bridge --help
mozyo --help
mozyo-bridge rules install
mozyo-bridge doctor --json
```

その後、`README.md` の `Beta Tester Install (GitHub main)` 節にある isolated target smoke を、GitHub `main`
install の代わりに該当 PyPI / TestPyPI install で実行する。特に ticket-ID entrypoint と scaffold guardrail の検証として、
Asana / Redmine / none の scaffold、`scaffold status`、`doctor --target` を release task に記録する。

## Trusted Publishing

TestPyPI pending publisher:

- Project: `mozyo-bridge`
- Owner: `hollySizzle`
- Repository: `mozyo_bridge`
- Workflow: `testpypi.yml`
- Environment: `testpypi`

PyPI production publisher:

- Project: `mozyo-bridge`
- Owner: `hollySizzle`
- Repository: `mozyo_bridge`
- Workflow: `publish.yml`
- Environment: `pypi`

## Internal Beta Gate

- 社内ベータ配布は TestPyPI を使う。production PyPI や GitHub Release は使わない。
- TestPyPI package が pipx で install でき、`mozyo-bridge` と `mozyo` を expose すること。
- beta tester には TestPyPI install command と version を明示する。
- stable version (`0.1.4` など) を TestPyPI に上げても、それだけでは production release ではない。

## Production Release Gate

- production release 前に `pipx install .` が動くこと。
- production PyPI 前に TestPyPI publish が成功していること。
- TestPyPI package が pipx で install でき、`mozyo-bridge` と `mozyo` を expose すること。
- GitHub Actions `Test` が Python 3.10, 3.11, 3.12, 3.13 で pass すること。
- production publish は local token upload ではなく `.github/workflows/publish.yml` を使うこと。
- GitHub Release は production publish の trigger なので、production 公開する時だけ作成する。

## Version Policy

- version scheme は PEP 440 と SemVer に従う。
- 現状 release-version mirror set は 2 file (`pyproject.toml` の `[project].version` + `src/mozyo_bridge/__init__.py` の `__version__`) で構成され、両者が同じ version 文字列を保つことで単一の正本とみなす。authoritative な mirror set 定義は `vibes/docs/logics/release-helper-contract.md` の `release bump` 節を正本とする。
- `tests/test_mozyo_bridge.py:test_module_version_matches_pyproject_version` がこの一致を test として enforce している。CLI 層からも `mozyo-bridge release bump --check` で同じ invariant が観測できる。
- pre-release は `0.1.0a1` (`a` = alpha)、`0.1.0b1` (beta)、`0.1.0rc1` (release candidate) の表記を使う。
- GA は `MAJOR.MINOR.PATCH` の三桁に揃える。例: `0.1.0`。
- patch release は後方互換 fix 専用で `Z` だけ上げる。例: `0.1.0` → `0.1.1` → `0.1.2`。
- 後方互換のある feature 追加は `Y` を上げ、`Z` を `0` に戻す。例: `0.1.2` → `0.2.0`。
- 公開 API を破る変更は `1.0.0` 以降で `X` を上げる。
- pre-release は production PyPI に publish しない。TestPyPI でだけ流通させる。

### 0.1.0a1 → 0.1.0 → patch の実行例

| 局面 | `pyproject.toml` の version | 公開先 |
| --- | --- | --- |
| 初回 alpha | `0.1.0a1` | TestPyPI のみ |
| GA | `0.1.0` | TestPyPI で rehearsal → production PyPI |
| 1回目の patch | `0.1.1` | TestPyPI で rehearsal → production PyPI |
| 2回目以降の patch | `0.1.2`, `0.1.3`, ... | 同上 |

## Version Bump

- code 変更と version bump を同じ commit に混ぜない。bump は単独 commit にする。
- bump 前に `main` の `Test` workflow が green であることを確認する。
- bump 後に再度 `Test` workflow の green を待ってから tag に進む。
- bump は authoritative な release-version mirror set 全 file (現状: `pyproject.toml` + `src/mozyo_bridge/__init__.py`) を同じ version 文字列に揃える。1 file だけ更新する形にしない。

手順:

1. `main` を最新にする (`git checkout main && git pull --ff-only`)。
2. mirror set 全 file の `version` を `X.Y.Z` に揃える。
   - Helper: `mozyo-bridge release bump --to X.Y.Z` を実行する。helper は contract が列挙する mirror set 全 file を 1 invocation で書き換え、worktree に diff として残す。helper は `git commit` / `git push` / `git tag` を一切実行しない。
   - mirror set 全 file の version 文字列が事前 grep / parser で見つからない (= mirror set が contract と乖離している) 場合は strict-fail する。partial 書き換えは発生しない (extract-all-then-write-all phase order)。
   - 同一 version を再指定した場合は no-op として扱う (`already at X.Y.Z`)。
3. `git diff` で書き換え内容を確認したうえで `git commit -m "Release vX.Y.Z"` を実行する。
4. `git push origin main`。
5. GitHub Actions `Test` の green を待つ。
   - Helper: `mozyo-bridge release workflow runs --workflow Test` で最新 run-id を確認し、`mozyo-bridge release workflow wait --run-id <id> --timeout <seconds>` で `completed` まで polling できる。

## Tag and Release

- tag は annotated tag を使い、`v` prefix を付ける。例: `v0.1.0`、`v0.1.0a1`。
- production publish workflow `.github/workflows/publish.yml` は `release: published` event で発火する。tag を push しただけでは発火しない。
- GitHub Release を published 状態にした瞬間が production publish の trigger になる。

GA / patch 手順:

1. version bump が main に入り `Test` が green であることを確認する。
   - Helper: `mozyo-bridge release workflow wait --run-id <id> --timeout <seconds>` で完了 polling、`mozyo-bridge release check workflow --run-id <id>` で `success` を観測する。
2. annotated tag を作って push する。

   ```bash
   git tag -a v0.1.0 -m "Release v0.1.0"
   git push origin v0.1.0
   ```

   helper は `git tag` を作成しない (`release bump` も `release publish` も tag 不可)。tag 作成は operator が直接 `git tag -a` で残す。

3. GitHub Release を作る。これが `publish.yml` を発火させる。
   `--verify-tag` で remote tag が存在しない場合は失敗させる。

   ```bash
   gh release create v0.1.0 --verify-tag --title "v0.1.0" --notes "<release notes>"
   ```

   Helper (release notes を file として準備した場合):

   ```bash
   mozyo-bridge release publish --pypi --tag v0.1.0 --notes-file <path>            # default dry-run
   mozyo-bridge release publish --pypi --tag v0.1.0 --notes-file <path> --execute  # 実行
   ```

   helper は default で dry-run であり、`--execute` を明示しない限り `gh release create` を呼ばない。release notes 文言は operator が用意した markdown file を渡す形に限定する (helper は notes を生成も書き換えもしない)。tag shape と `--notes-file` path が validate される。

4. `Publish to PyPI` workflow が成功したことを確認する。
   - Helper: `mozyo-bridge release workflow wait --run-id <id> --timeout <seconds>` で polling できる。
5. `pipx install --force mozyo-bridge==X.Y.Z` で fresh install を確認する。
6. fresh install 後、`README.md` の `Beta Tester Install (GitHub main)` 節に書かれた acceptance smoke を、GitHub `main` install のかわりに本 PyPI install に対して実行する (`mozyo-bridge rules install` → skill install → `mozyo-bridge doctor` → isolated target に Asana / Redmine の scaffold + `scaffold status` + `doctor --target`)。tester / CI 検証 1 command として `mozyo-bridge doctor --json` を使い、`ok=true` と scaffold section ok を確認する。

### Pre-release (0.1.0a1)

- pre-release は production publish を起こしてはならないので、GitHub Release を作らない。
- bump → push → `Publish to TestPyPI` workflow を `workflow_dispatch` で起動する流れだけで完了する。
  - Helper: `mozyo-bridge release publish --testpypi --version 0.1.0a1` が `gh workflow run testpypi.yml --ref main -f version=0.1.0a1` 相当の dispatch を行い、続けて run-id を Asana task に貼れる shape で stdout に出す。polling は `mozyo-bridge release workflow wait --run-id <id> --timeout <seconds>` に明示的に委ねる。
- 検証は `pipx install --backend pip --index-url https://test.pypi.org/simple/ --pip-args "--extra-index-url https://pypi.org/simple/" mozyo-bridge==0.1.0a1` で行い、続けて `README.md` の `Beta Tester Install (GitHub main)` 節の acceptance smoke (rules install → skill install → `mozyo-bridge doctor` → isolated target に対する Asana / Redmine scaffold + doctor) を TestPyPI install に対して実行する。
- `pipx` が default backend に `uv` を使う環境では、TestPyPI の `--index-url` と dependency 用 `--extra-index-url` の組み合わせが期待通り解決されないことがあるため、TestPyPI 検証では `--backend pip` を明示する。
- 必要なら `git tag -a v0.1.0a1 -m "Pre-release v0.1.0a1"` で tag を打って push する。GitHub Release は作らない。

`mozyo-bridge release publish --plan` は現在の git ref / pyproject version / 最新 `Test` workflow conclusion / TestPyPI 既存 version の有無を at-a-glance で集約する。release を進めるか / pre-release に留めるかの判定は引き続き operator が行う (helper は GA / beta / patch を判定しない)。

### Patch Release

1. fix を main に merge し `Test` を pass させる。
   - Helper: `mozyo-bridge release workflow wait --run-id <id> --timeout <seconds>` で polling できる。
2. Version Bump 手順で `Z` を 1 上げて単独 commit する (`mozyo-bridge release bump --to X.Y.Z` → `git commit -m "Release vX.Y.Z"` → `git push`)。
3. 必要なら `Publish to TestPyPI` workflow で TestPyPI rehearsal を行う (`mozyo-bridge release publish --testpypi --version X.Y.Z`)。
4. Tag and Release 手順で GitHub Release を作る (`git tag -a vX.Y.Z` → `mozyo-bridge release publish --pypi --tag vX.Y.Z --notes-file <path> --execute`)。

### Rollback

- production publish 前に問題が出た場合は GitHub Release を draft に戻すか delete し、tag を削除する。

  ```bash
  git push --delete origin vX.Y.Z
  git tag -d vX.Y.Z
  ```

  rollback は helper の admit する surface から除外されている (`vibes/docs/logics/release-helper-contract.md` の `Never mutate implicitly` 節)。`release-flow.md` の本節を operator が手で実行する。

- 既に production PyPI に publish された version は取り下げない。次の patch を上げて修正版を出す。
- TestPyPI に上げた pre-release は再 upload できないので、修正は次の `aN` を上げて出す (`0.1.0a1` → `0.1.0a2`)。
