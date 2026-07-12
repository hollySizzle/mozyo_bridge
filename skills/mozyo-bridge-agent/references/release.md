# Release リファレンス

## 適用範囲: mozyo-bridge package maintainer 専用

本 reference は **mozyo-bridge package 自体** の release runbook である — `hollySizzle/mozyo_bridge` の versioning、TestPyPI / PyPI publishing、distribution check を扱う。採用 project がこの package を publish することはなく、自身の release にこの runbook を適用しない。project 自身の release process を使い、portable な姿勢のみを保つ (release の risk に見合った検証を実行し、local token upload より OIDC Trusted Publishing を優先する)。runbook は maintainer session と dogfooding session が携行できるよう配布本文に残す。

## Versioning Policy

mozyo-bridge package version は semantic versioning に従う。segment の意味は次で固定する。

- **patch** (`x.y.Z`): 後方互換な fix。契約 (CLI surface / API / preset) を変えない bug fix と内部修正。
- **minor** (`x.Y.0`): feature 追加、または backend capability の拡張。後方互換を保つ。
- **major** (`X.0.0`): breaking contract。既存の CLI / API / preset 契約を後方非互換に変更する。

次の feature release から **`0.10` 系** に入る (feature 追加のため minor bump)。

- **Redmine Version (`#308` 等) は roadmap bucket** であり、package version でも release authority でもない。roadmap の grouping と、実際に出荷する package version / tag は別物として扱う。issue を Redmine Version に割り当てても、それが特定の package version を確約するわけではない。
- herdr adapter のような feature は minor-release 候補だが、実際の package version は **下記の release gate でのみ決定** する。roadmap や本 policy 文書が version 番号を先取りして固定することはない。
- version の **決定・bump・tag・TestPyPI / PyPI publication・GitHub Release は、本 policy 成文化では行わない**。それらは下記「Release フロー」「Distribution gate」の release gate でのみ行う。

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
- Version: job は `scripts/compute_testpypi_dev_version.py --write` を実行し、commit 済みの release version に PEP 440 の `.dev<N>` segment を付加する。書き換え対象は canonical release-version mirror set 全体 (`pyproject.toml` の `[project].version` と `src/mozyo_bridge/__init__.py` の `__version__`) であり、両 file を同一の exact dev version へ揃える。これにより wheel METADATA と runtime `__version__` (ひいては `mozyo-bridge --version` / `mozyo --version`) が一致する (Redmine #13586。以前は `pyproject.toml` だけ書き換えたため両者が食い違っていた)。mirror set は hardcode せず `vibes/docs/logics/release-helper-contract.md` から読み、`mozyo-bridge release bump` と同じ stdlib-only primitive (release version-governance Feature package の `version_mirror` module: `src/mozyo_bridge/e_130_governance_distribution/f_160_release_version_governance/application/version_mirror.py`) を再利用する。shared kernel は凍結 (Redmine #12640) のため `shared/` には置かない。`N` は UTC timestamp と、トリガーとなった `Test` run の globally-unique な id を連結したものであり (例: `0.9.2.dev20260628090000123456789`)、同一秒に完了した 2 つの `Test` run でも異なる version を生成し、TestPyPI 上で決して衝突しない。書き換えは pre-write validation 後にのみ全 file を更新する two-phase であり (base が mirror 間で不一致、または literal 欠落なら両 file 不変で fail)、CI checkout 内の一時的なもので決して commit されないため、commit 済みの release version には触れない。
- Auth: GitHub Actions Trusted Publishing / OIDC。`testpypi.yml` は build job と publish job に分離され (Redmine #13601)、`id-token: write` + `environment: testpypi` を持つのは publish job だけである。publish job は build job が上げた artifact を download して upload するだけで、checkout / build / verify は trusted な build job 側 (OIDC credential なし) に閉じる。既存の TestPyPI pending publisher (workflow `testpypi.yml`) がそのまま authorize し続ける。local の PyPI token は使わない。
- Evidence: dev-publish (自動) path は `version` と `commit` SHA (加えて source CI run の URL) を workflow run の job summary に書き込む。対応関係はそこで読む。exact-candidate (手動) path も `version` / `source_sha` / `source_ref` を job summary に書く。

## TestPyPI exact-candidate 手動配布 (internal beta, Redmine #13601)

内部 beta の手動配布は `origin/main` promotion や Redmine Version close を先に要求しない。`Version close → origin/main → TestPyPI → #13528/#13527 → Version close` の循環を壊すため、exact reviewed integration head を **main 固定の workflow 定義** から直接 publish する非循環 gate を使う。

- workflow の定義 / event ref は `main` 固定である。任意の staging ref を workflow authority として実行しない (それを許すと staging ref の workflow 定義が OIDC を要求できてしまう)。
- artifact authority は exact `source_sha`、release approval authority は Redmine gate + 外部 `testpypi` environment protection (owner required reviewer + main-only deployment branch policy) であり、3 者を分離する。
- 手動 `workflow_dispatch` は required inputs を取る: exact 40-hex `source_sha`、`expected_version`、approved origin ref の `source_ref` (action-time に exact `source_sha` へ解決すること。ancestor-only / local-only SHA は不可)、correlation 用の `dispatch_nonce`。
- trusted な build job が dispatch 前/実行内で fail-closed 照合する: HEAD == `source_sha`、`source_ref` の origin tip == `source_sha`、2-file version mirror == `expected_version`、同 SHA の `Test` workflow (`test.yml`) が `completed` + `success`、`expected_version` が TestPyPI 未使用 (lookup 不能は fail-closed)。
- helper: `mozyo-bridge release publish --testpypi --source-sha <40-hex> --expected-version <X.Y.Z> --source-ref <origin ref>` が `gh workflow run testpypi.yml --ref main -f source_sha=... -f expected_version=... -f source_ref=... -f dispatch_nonce=...` を構成し、run-name 中の nonce で dispatch と run を決定的に相関する (latest-one 推測はしない。exact 1 件以外は fail-closed)。
- 順序: #13528「TestPyPI publish」→ #13527「exact install QA」。exact install QA は publish 後に `scripts/install_testpypi_dev.sh <exact version>` で行い、`origin/main` promotion / Version close を前提にしない。
- automatic main-CI dev publish path は後方互換に維持する (owner 承認済みの `testpypi` required reviewer 導入後は automatic path も deployment approval 待ちになる — これは意図的な OIDC protection 優先の変更)。
- 外部 environment 変更 (required reviewer / deployment branch policy) と実際の dispatch は、implementation review green 後に owner action として分離する。

production PyPI は分離されたままである。この workflow は production PyPI へ publish せず、tag も打たず、GitHub Release も決して作成しない (production の `publish.yml` は `release: published` で走る)。

## Local pipx dev runtime の整合

通常 PATH の pipx runtime (default `~/.local/bin/mozyo-bridge`) を publish 済みの TestPyPI dev artifact に整合させ、その上で CLI surface を検証する — `--version` 単独ではなく:

```bash
# Pin the EXACT version from the 'Publish to TestPyPI' run summary
scripts/install_testpypi_dev.sh 0.9.2.dev20260628090000123456789
```

正確な dev version を渡す。`latest` は意図的に非サポートである。install は TestPyPI を primary index として、PyPI を依存関係用の extra-index として使う。pip は target package を両方の index から考慮し、dev release は PyPI の final より順序が前に sort されるため、pin なしの install は PyPI の production release を解決して smoke 証跡を汚染しうる。正確な dev version は TestPyPI にのみ存在する (PyPI は dev release を決して host しない) ため、pin することで artifact が TestPyPI 由来であることが保証される。

script は pip backend で install し (`mozyo-bridge` は TestPyPI、依存関係は PyPI、`--pre`、`--force`)、install された surface を検証する:

- `mozyo-bridge --version` と `mozyo --version` (必須)。両 CLI が報告する version は、pin した exact dev version と **厳密一致** を assert する。どちらかが不一致なら script は nonzero で停止する (install された artifact が pin した build でないため、smoke 証跡を誤って別 build に結び付けない — Redmine #13586)。
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
