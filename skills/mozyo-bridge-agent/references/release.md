# Release リファレンス

## 適用範囲: mozyo-bridge package maintainer 専用

本 reference は **mozyo-bridge package 自体** の release runbook である — `hollySizzle/mozyo_bridge` の versioning、TestPyPI / PyPI publishing、distribution check を扱う。採用 project がこの package を publish することはなく、自身の release にこの runbook を適用しない。project 自身の release process を使い、portable な姿勢のみを保つ (release の risk に見合った検証を実行し、local token upload より OIDC Trusted Publishing を優先する)。runbook は maintainer session と dogfooding session が携行できるよう配布本文に残す。

## 標準検証

変更に見合った最小の check set を使う。

```bash
python -m unittest discover -s tests -v
python -m pip wheel . --no-deps -w /tmp/mozyo_bridge_dist
python -m mozyo_bridge --help
```

local test には、project がサポートする Python version に一致した Python 環境を使う。

## tmux delivery 変更時

tmux delivery、pane 解決、marker safety、CLI 通知契約を変更するときは、実 smoke check を実行する。

```bash
python smoke/real_tmux_notify_smoke.py
MOZYO_BRIDGE_COMMAND=mozyo-bridge-testpypi python smoke/real_tmux_notify_smoke.py
```

## Release フロー

1. active な ticket システムの release ticket から始める (`mozyo_bridge` では Redmine issue、Asana-preset repo では Asana task)。
2. local の unit test と build check を実行する。
3. release artifact guardrail を実行する。
4. `main` へ push し、GitHub Actions `Test` の成功を確認する。
5. TestPyPI には `Publish to TestPyPI` を使う。
6. TestPyPI install を `pipx` で検証する。
7. 内部 beta distribution は TestPyPI install の検証後に完了として扱う。
8. production PyPI release は別途、明示的に要求された場合にのみ決定する。

## TestPyPI dev 自動配布 (main CI)

`.github/workflows/testpypi.yml` は、`main` で `Test` workflow が成功した後に、一意な TestPyPI dev artifact を自動で publish する (Redmine #12756)。これにより、source-runtime や `PYTHONPATH=src` に依存する代わりに、実 smoke 作業 (例: #12709) 向けに、通常 PATH で install 可能な artifact を `main` に整合させ続ける。

- Trigger: `Test` に対する `workflow_run` (`completed`, `branches: [main]`)。job は `workflow_run.conclusion == 'success'` のときにのみ publish する。
- Version: job は `scripts/compute_testpypi_dev_version.py` を実行し、commit 済みの `pyproject.toml` version に PEP 440 の `.dev<N>` segment を付加する。`N` は UTC timestamp と、トリガーとなった `Test` run の globally-unique な id を連結したものであり (例: `0.9.2.dev20260628090000123456789`)、同一秒に完了した 2 つの `Test` run でも異なる version を生成し、TestPyPI 上で決して衝突しない。この書き換えは CI checkout 内の一時的なもので決して commit されないため、commit 済みの release version には触れない。
- Auth: GitHub Actions Trusted Publishing / OIDC (`environment: testpypi`, `id-token: write`)。自動経路は manual dispatch と同じ `testpypi.yml` workflow file に置かれ、既存の TestPyPI pending publisher (workflow `testpypi.yml`) がそのまま authorize し続ける。local の PyPI token は使わない。
- 手動の `workflow_dispatch` は不変である: exact-version の release-candidate 検証のために、commit 済みの (static な) release version を build する。
- Evidence: dev-publish job は `version` と `commit` SHA (加えて source CI run の URL) を workflow run の job summary に書き込む。対応関係はそこで読む。

production PyPI は分離されたままである。この workflow は production PyPI へ publish せず、tag も打たず、GitHub Release も決して作成しない (production の `publish.yml` は `release: published` で走る)。

## Local pipx dev runtime の整合

通常 PATH の pipx runtime (default `~/.local/bin/mozyo-bridge`) を publish 済みの TestPyPI dev artifact に整合させ、その上で CLI surface を検証する — `--version` 単独ではなく:

```bash
# Pin the EXACT version from the 'Publish to TestPyPI' run summary
scripts/install_testpypi_dev.sh 0.9.2.dev20260628090000123456789
```

正確な dev version を渡す。`latest` は意図的に非サポートである。install は TestPyPI を primary index として、PyPI を依存関係用の extra-index として使う。pip は target package を両方の index から考慮し、dev release は PyPI の final より順序が前に sort されるため、pin なしの install は PyPI の production release を解決して smoke 証跡を汚染しうる。正確な dev version は TestPyPI にのみ存在する (PyPI は dev release を決して host しない) ため、pin することで artifact が TestPyPI 由来であることが保証される。

script は pip backend で install し (`mozyo-bridge` は TestPyPI、依存関係は PyPI、`--pre`、`--force`)、install された surface を検証する:

- `mozyo-bridge --version` と `mozyo --version` (必須)
- `mozyo-bridge project-gateway consult --help` (必須)
- `mozyo-bridge workflow step --help` (将来の #12755 — それが出荷される前に build された artifact に対しては PENDING として報告され、failure ではない)

smoke 証跡を commit に結び付けるには: install された `mozyo-bridge --version` 文字列と、それが対応する commit SHA (`Publish to TestPyPI` run summary から) の両方を、Redmine の smoke-evidence journal に記録する。version 単独では不十分である — 異なる `main`、TestPyPI、PyPI の build は、異なる command/preset/skill 内容を出荷しながら同じ base version 文字列を共有しうる。

## Release artifact guardrail

`mozyo-bridge --version` 単独に依拠しない。これは `pyproject.toml` の package version を報告するため、GitHub `main`、TestPyPI、PyPI は、異なる command、preset、skill 内容を出荷しながら同じ version 文字列を共有しうる。

release 前に、3 つの surface すべてを検査する:

- Source tree: credential、token、`.env` / `.pypirc` の内容、`/Users/<name>`、`/home/<name>`、`C:\Users\<name>` のような host 固有の絶対 path を検索する。個人の home path は、secret でない場合でも public ref では release blocker である。
- Fresh scaffold 出力: 隔離した `--home` と隔離した target で、`rules install` を実行し、`asana`、`redmine`、`none` を scaffold し、その上で生成された `AGENTS.md`、`CLAUDE.md`、`.mozyo-bridge/scaffold.json` が `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/agent-workflow.md` を含み、解決済みの user-home path を含まないことを確認する。`scaffold status` は clean を報告しなければならない。
- Build artifact: wheel と sdist の両方を build して展開し、展開された file を scan する。wheel だけを検査しない。sdist は root docs を含みうる。

false positive とその根拠は、active な release ticket に記録する (repo の central preset に応じて、release issue 上の Redmine journal、または release task 上の Asana comment)。

## Release ref の整合

- version bump は単独 commit として保つ。
- tag 付き release では、install script と skill tree を、検証対象の package version と同じ tag から取得しなければならない。fresh install smoke には `MOZYO_BRIDGE_SKILL_REF=vX.Y.Z` を設定する。
- remote tag が意図した release commit を指すことを `git ls-remote origin refs/tags/vX.Y.Z` で確認する。
- release acceptance を主張する際に、TestPyPI / PyPI package と floating な `main` 由来の install script を混在させない。

TestPyPI 検証では pip backend を強制し、`mozyo-bridge` には TestPyPI を使い、依存関係には PyPI を利用可能なままにする:

```bash
pipx install --backend pip --index-url https://test.pypi.org/simple/ --pip-args "--extra-index-url https://pypi.org/simple/" mozyo-bridge==X.Y.Z
```

内部 beta distribution のために GitHub Release を作成しない。production の publish workflow は `release: published` で走るため、GitHub Release は production trigger である。

## Distribution gate

- 内部 beta distribution は production PyPI ではなく TestPyPI を使う。
- 内部 beta を ready と呼ぶ前に、beta tester に渡すのと同じ command で package を TestPyPI から install する。
- beta tester の経路を、local checkout、editable install、local wheel で代替しない。
- 両方の command entry point が起動することを確認する: `mozyo-bridge --help` と `mozyo --help`。
- 変更に関わる配布 scaffold/rule 内容が、install 済み package の内部に存在することを確認する。
- `rules install`、per-preset の scaffold、`scaffold status`、`doctor --target` が、fresh な TestPyPI / PyPI install 経路から動作することを確認する。
- production PyPI distribution は内部 beta distribution と分離されており、明示的な production release の要求または承認を要する。

## Trusted Publishing

TestPyPI の pending publisher:

- Project: `mozyo-bridge`
- Owner: `hollySizzle`
- Repository: `mozyo_bridge`
- Workflow: `testpypi.yml`
- Environment: `testpypi`

PyPI の production publisher:

- Project: `mozyo-bridge`
- Owner: `hollySizzle`
- Repository: `mozyo_bridge`
- Workflow: `publish.yml`
- Environment: `pypi`
