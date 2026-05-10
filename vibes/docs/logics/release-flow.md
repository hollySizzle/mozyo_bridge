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
3. `main` に push し、GitHub Actions `Test` の成功を確認する。
4. TestPyPI は `Publish to TestPyPI` workflow を使う。
5. TestPyPI install を `pipx` で検証する。
6. 社内ベータ配布は TestPyPI install 検証で完了とする。
7. production PyPI 公開は、別途 production release として明示判断する。

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

### Pre-release (0.1.0a1)

- pre-release は production publish を起こしてはならないので、GitHub Release を作らない。
- bump → push → `Publish to TestPyPI` workflow を `workflow_dispatch` で起動する流れだけで完了する。
- 検証は `pipx install --backend pip --index-url https://test.pypi.org/simple/ --pip-args "--extra-index-url https://pypi.org/simple/" mozyo-bridge==0.1.0a1` で行う。
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
