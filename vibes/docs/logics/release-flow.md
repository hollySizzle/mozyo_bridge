# Release And Verification Logic

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

### Release Ref Consistency

docs / command / scaffold preset / skill archive が同じ release commit を指すことを確認する。

- `pyproject.toml` の version bump は単独 commit にする。
- `mozyo-bridge --version` は package version だけを示す。GitHub `main` と PyPI artifact が同じ version string を持つ場合があるため、差分確認には使わない。
- TestPyPI / PyPI の fresh install smoke は、package version と同じ git ref から install scripts / skill tree を取得する。tag release なら `MOZYO_BRIDGE_SKILL_REF=vX.Y.Z` を使う。
- tag は release commit を指す annotated tag として作成し、tag 作成後に `git ls-remote origin refs/tags/vX.Y.Z` で remote ref を確認する。

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

- version scheme は PEP 440 と SemVer に従う。`pyproject.toml` の `[project].version` が単一の正本である。
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

手順:

1. `main` を最新にする (`git checkout main && git pull --ff-only`)。
2. `pyproject.toml` の `version` を更新する。
3. `git commit -m "Release vX.Y.Z"` で単独 commit する。
4. `git push origin main`。
5. GitHub Actions `Test` の green を待つ。

## Tag and Release

- tag は annotated tag を使い、`v` prefix を付ける。例: `v0.1.0`、`v0.1.0a1`。
- production publish workflow `.github/workflows/publish.yml` は `release: published` event で発火する。tag を push しただけでは発火しない。
- GitHub Release を published 状態にした瞬間が production publish の trigger になる。

GA / patch 手順:

1. version bump が main に入り `Test` が green であることを確認する。
2. annotated tag を作って push する。

   ```bash
   git tag -a v0.1.0 -m "Release v0.1.0"
   git push origin v0.1.0
   ```

3. GitHub Release を作る。これが `publish.yml` を発火させる。
   `--verify-tag` で remote tag が存在しない場合は失敗させる。

   ```bash
   gh release create v0.1.0 --verify-tag --title "v0.1.0" --notes "<release notes>"
   ```

4. `Publish to PyPI` workflow が成功したことを確認する。
5. `pipx install --force mozyo-bridge==X.Y.Z` で fresh install を確認する。
6. fresh install 後、`README.md` の `Beta Tester Install (GitHub main)` 節に書かれた acceptance smoke を、GitHub `main` install のかわりに本 PyPI install に対して実行する (`mozyo-bridge rules install` → skill install → `mozyo-bridge doctor` → isolated target に Asana / Redmine の scaffold + `scaffold status` + `doctor --target`)。tester / CI 検証 1 command として `mozyo-bridge doctor --json` を使い、`ok=true` と scaffold section ok を確認する。

### Pre-release (0.1.0a1)

- pre-release は production publish を起こしてはならないので、GitHub Release を作らない。
- bump → push → `Publish to TestPyPI` workflow を `workflow_dispatch` で起動する流れだけで完了する。
- 検証は `pipx install --backend pip --index-url https://test.pypi.org/simple/ --pip-args "--extra-index-url https://pypi.org/simple/" mozyo-bridge==0.1.0a1` で行い、続けて `README.md` の `Beta Tester Install (GitHub main)` 節の acceptance smoke (rules install → skill install → `mozyo-bridge doctor` → isolated target に対する Asana / Redmine scaffold + doctor) を TestPyPI install に対して実行する。
- `pipx` が default backend に `uv` を使う環境では、TestPyPI の `--index-url` と dependency 用 `--extra-index-url` の組み合わせが期待通り解決されないことがあるため、TestPyPI 検証では `--backend pip` を明示する。
- 必要なら `git tag -a v0.1.0a1 -m "Pre-release v0.1.0a1"` で tag を打って push する。GitHub Release は作らない。

### Patch Release

1. fix を main に merge し `Test` を pass させる。
2. Version Bump 手順で `Z` を 1 上げて単独 commit する。
3. 必要なら `Publish to TestPyPI` workflow で TestPyPI rehearsal を行う。
4. Tag and Release 手順で GitHub Release を作る。

### Rollback

- production publish 前に問題が出た場合は GitHub Release を draft に戻すか delete し、tag を削除する。

  ```bash
  git push --delete origin vX.Y.Z
  git tag -d vX.Y.Z
  ```

- 既に production PyPI に publish された version は取り下げない。次の patch を上げて修正版を出す。
- TestPyPI に上げた pre-release は再 upload できないので、修正は次の `aN` を上げて出す (`0.1.0a1` → `0.1.0a2`)。
