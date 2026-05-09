# Release And Verification Logic

## 通常確認

変更内容に応じて、必要最小限の verification を選ぶ。

```bash
python -m unittest discover -s tests -v
python -m pip wheel . --no-deps -w /tmp/mozyo_bridge_dist
python -m mozyo_bridge --help
```

この machine では Homebrew の `python3` が Python 3.14 を指し、`PyYAML` が入っていない場合がある。local test は dependency が入った venv を使う。

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
6. その後で production PyPI へ公開するか判断する。

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

## Release Gate

- production release 前に `pipx install .` が動くこと。
- production PyPI 前に TestPyPI publish が成功していること。
- TestPyPI package が pipx で install でき、`mozyo-bridge` と `mozyo` を expose すること。
- GitHub Actions `Test` が Python 3.10, 3.11, 3.12, 3.13 で pass すること。
- production publish は local token upload ではなく `.github/workflows/publish.yml` を使うこと。
