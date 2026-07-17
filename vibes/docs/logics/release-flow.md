# Release And Verification Logic

## Helper-Driven Execution Overview

`mozyo-bridge release …` helper family は本 doc に記述された mechanical step を CLI として再現する read-only / bounded-mutation な薄い wrapper である。helper の admit する surface と human-vs-script boundary は `vibes/docs/logics/release-helper-contract.md` を正本とする。本 doc は判断付きの release step (GA / beta judgment, release notes wording, blocker disposition) を helper 化しない。

helper invocation を以下の節で各 step に併記する。helper を呼ばずに manual command を直接実行する従来手順も引き続き有効であり、helper はそれと等価な mechanical step を 1 command に括り出すための facade である。active release ticket (Redmine journal / Asana comment; preset に従う) が引き続き durable な作業ログであり、helper stdout は durable record の代替にならない。

| step | helper subcommand | judgment 残し方 |
| --- | --- | --- |
| Source Tree Hygiene | `mozyo-bridge release check tree` | classifier 後の blocker disposition は operator |
| Fresh Scaffold Smoke | `mozyo-bridge release check scaffold` | strict-fail; preset 修正で再実行 |
| Build Artifact Inspection | `mozyo-bridge release check artifact` | classifier 後の blocker disposition は operator |
| Canonical Renderer / Plugin Mirror Drift | `mozyo-bridge release check drift` | strict-fail; canonical 再 render または mirror 再 sync で復旧 |
| Release Ref Consistency (mirror set 内 version 確認) | `mozyo-bridge release bump --check` | mirror set 不一致は strict-fail |
| GitHub Actions run status / conclusion 確認 | `mozyo-bridge release check workflow --run-id <id>` | `failure` 受領可否は operator |
| GitHub Actions run polling | `mozyo-bridge release workflow wait --run-id <id> --timeout <s>` | timeout exit (124) は operator が次手を決定 |
| Version Bump (mirror set 全 file の version 書き換え) | `mozyo-bridge release bump --to X.Y.Z` | `git commit` / `git push` / `git tag` は operator |
| TestPyPI publish workflow dispatch (exact candidate) | `mozyo-bridge release publish --testpypi --source-sha <40-hex> --expected-version X.Y.Z --source-ref refs/heads/<branch>` | dispatch 後の判定は operator (Redmine #13601)。`--source-ref` は origin 上の ref literal (local の `origin/<branch>` 表記は dispatch 前に reject, Redmine #13883) |
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

`smoke/real_tmux_notify_smoke.py` は **explicit な strict `--mode standard` rail** のみを自動検証する (v0.4 contract pivot 後は `mozyo-bridge handoff send --mode standard --force` を非 agent な `sh` receiver に対して打つ形)。v0.4 で normative default になった `--mode queue-enter` rail (Asana `1214825156046950` 配下、`vibes/docs/logics/tmux-send-safety-contract.md` の `## Default Delivery Promise (v0.4)` / `## Queue-Enter Default Rail` を正本とする) は (a) real Claude / Codex TUI 上の prompt queue 挙動と pane metadata に依存し、(b) Layer B deterministic preflight (`--force` 不可、window-name / same-session / active-split / per-receiver foreground process allowlist) が non-agent `sh` receiver を typing 前に reject するため、同 smoke では auto-cover できない。v0.4 default を触る変更は同 smoke header の docstring に記載した手順 (`mozyo-bridge handoff send` を `--mode` 指定なしで queue-enter default として marker 観測あり / 観測なしの 2 ケース、`--mode standard` 明示で strict regression 1 ケース、v0.3 preflight spot-check 3 ケース (foreign-session / inactive-split / non-agent reject)、v0.4 force-rejection regression 1 ケースの計 7 ケース) を Asana task に記録する。default promise は `confirmed landing` ではなく `strong preflight 付き practical queued submission` のため、queue-enter rail の auto smoke が無い状態でも product 約束自体は破綻しない (durable record が引き続き source of truth)。

### Handoff primitive regression coverage (Asana `1214760806178471`)

`mozyo-bridge handoff send` / `handoff reply` / 上位 alias `mozyo-bridge reply` の primitive regression は in-process unit test で固定済みであり、release 前に published package 専用 smoke を追加する必要はない。具体的カバー:

- `HandoffOrchestratorTest` — standard mode の marker observed + Enter、`pending` mode (Enter 発行せず operator owned)、strict mode marker_timeout `C-u` rollback、anchor / target / non-agent pane の各 invalid 分岐。
- `RelaxedQueueEnterRailTest` — CLI `--mode` の受理 / 未指定時の v0.4 default 確認 (`queue-enter`)、queue-enter rail observed/unobserved marker の outcome、strict rail rollback、`--force` rejection、target window guard、v0.3 preflight (foreign-session / inactive-split / cross-receiver / weak identity admit)、delivery record の operator-note 文言。
- `NotifyContractTest` — `notify-codex` / `notify-claude` / `notify-codex-review` / `notify-claude-review-result` が primitive を経由していること (marker shape / body / Enter 発行 / structured outcome)、queue-enter default で marker 未観測でも Enter が出ること、success line 互換、`--record-format` / `--record-command` の伝搬、および `notify-claude-legacy-task` が primitive 経路に乗らず structured outcome を emit しないこと (retired-queue cleanup wrapper の境界)。
- `DeliveryRecordTest` + `HandoffRecordEmissionTest` — sent / pending_input / blocked / target_unavailable / target_not_agent / invalid_anchor / invalid_args の各 outcome に対する markdown record + JSON outcome の決定論的生成、`--record-format both|text|json` の組み合わせ、`--record-command` の inline 化。
- `SharedSkillWorkflowTest::test_workflow_lifecycle_anchors_at_handoff_primitive` + `ScaffoldPresetHandoffPrimitiveDocsTest` — skill workflow.md と asana / redmine scaffold preset の `agent-workflow.md` / `CLAUDE.md` / `AGENTS.md` が primitive を standard path として記述し、`read` / `message` / `type` / `keys` を operator/debug primitive として明記し、`status` / `doctor` / pane scrollback からの推論を禁止する文言を保持していることを doc-regression として固定する。`Standard notification command: mozyo-bridge notify-* --issue --journal` 等の旧 standard wording が再導入されればここで落ちる。

Published-package 専用 smoke は追加しない。理由:

- TestPyPI / PyPI fresh install acceptance (`Beta Tester Install` 節) が installed binary に対して `mozyo-bridge doctor` を実行する。primitive subcommand (`handoff send` / `handoff reply` / `reply` alias / `notify-*` standard variants) が installed binary に欠落していれば parser 構築段階で fail し、`scaffold apply` / `scaffold status` フローまで到達できない。
- 同じ acceptance flow が installed binary に対して `scaffold apply <preset>` を実行するため、scaffold preset の中身 (上記 `ScaffoldPresetHandoffPrimitiveDocsTest` で固定された primitive guidance を含む) が installed package に乗っていることが確認される。
- queue-enter rail の **末端 tmux 挙動** は real Claude / Codex TUI に依存し、`sh` receiver に対しては典型的な smoke にできない (上記 7 ケース手順を Asana task に残す運用で代替する)。`--mode standard` 鉄道は本 smoke で end-to-end 検証済み。

新規 smoke を増やす条件: real TUI receiver を伴う queue-enter 自動化が確立した時、または `notify-*` wrapper が primitive 経路を離れた時 (上記 doc-regression テストが落ちて初めて気づくのでは遅いケース)。両条件は現状 false であるため smoke の追加は deferred とする。

## Release Flow

1. active release ticket から開始する (Redmine governed preset では Redmine issue / journal、Asana governed preset では Asana task / comment)。
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
git ls-files -z .env '.env.*' .pypirc | tr '\0' '\n' | grep -vx '.env.example' && exit 1
git grep -nE '/Users/[A-Za-z0-9._-]+/|/home/[A-Za-z0-9._-]+/|C:\\Users\\[A-Za-z0-9._-]+\\' -- \
  ':!*.pyc' ':!build' ':!dist' ':!.git' ':!.venv' ':!tmp'
git grep -nEi '(^|[^[:alnum:]_])(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)[[:space:]]*[:=][[:space:]]*[^<[:space:]#][^[:space:]#]*|(^|[^[:alnum:]_])(ASANA|GITHUB|PYPI|TWINE|REDMINE)[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|KEY)[[:space:]]*[:=][[:space:]]*[^<[:space:]#][^[:space:]#]*' -- \
  ':!*.pyc' ':!build' ':!dist' ':!.git' ':!.venv' ':!tmp'
```

`git grep` は説明文中の `token` / `secret` という単語だけでは blocker にしない。credential らしい代入形、tracked `.env` / `.pypirc`、実パスらしい personal home path を candidate として扱う。candidate のうち、env read / type annotation / identifier reference / placeholder / test sentinel など safe-code pattern は helper の second-stage classifier で除外する。placeholder ではない literal credential value は blocker であり、token punctuation (`.`, `/`, `+`, `=`, `_`, `-`) を含む値も blocker として扱う。classifier 後に残った blocker は clean に直すか、設計上残すなら active release ticket に disposition と理由を記録してから進める。
`/Users/<name>` のような個人ホーム絶対パスが router、skill、docs、scaffold preset、manifest に入っている場合は release しない。

Helper:

```bash
mozyo-bridge release check tree
```

上記の `git status` / `git log -S'/Users/'` / `git grep` を 1 command として再現する。同じ pathspec exclusion を内側で適用し、personal path は strict-fail (exit 1) で返す。secret-shape candidate は second-stage classifier を通し、safe-code pattern は helper が除外し、real credential literal と判断できるものだけを strict-fail にする。classifier 後の blocker を既知 drift として流さず、operator は clean に直すか active release ticket に disposition を残してから release を進める。

### Fresh Scaffold Smoke

local checkout から、isolated home / isolated target に対して全 preset を fresh scaffold する。
生成物に host 固有パスが入らず、portable rule path と `scaffold status` が揃うことを確認する。

```bash
tmp="$(mktemp -d)"
home="$tmp/home"
python -m mozyo_bridge rules install --home "$home"
for preset in $(python - <<'PY'
from mozyo_bridge.scaffold.rules import PRESETS
print(" ".join(PRESETS))
PY
); do
  project="$tmp/project-$preset"
  mkdir -p "$project"
  python -m mozyo_bridge scaffold apply "$preset" --target "$project" --home "$home"
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

上記 loop を 1 command で再現する。helper は tmp home / tmp target を都度新規に確保し、`presets.yaml` の全 preset について host-path leak / portable rule path / `scaffold status: clean` を検査する。tmp 領域は helper 完了時に自動で破棄され、repo の worktree や `~/.mozyo_bridge` は触らない。

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
find "$tmp" \( -name '.env' -o -name '.env.*' -o -name '.pypirc' \) ! -name '.env.example' -print -quit | grep . && exit 1
rg -n '/Users/[A-Za-z0-9._-]+/|/home/[A-Za-z0-9._-]+/|C:\\Users\\[A-Za-z0-9._-]+\\|\\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)\\b\\s*[:=]\\s*[^<\\s#][^\\s#]*|\\b(ASANA|GITHUB|PYPI|TWINE|REDMINE)[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|KEY)\\b\\s*[:=]\\s*[^<\\s#][^\\s#]*' "$tmp" && exit 1
```

この検査は説明文中の `token` / `secret` という単語だけでは blocker にしない。artifact 内の credential-shape candidate も source tree と同じ classifier semantics で扱い、safe-code pattern は除外し、real credential literal / tracked secret file / personal path は blocker にする。classifier 後に blocker が残る場合は clean に直すか、active release ticket に artifact path と disposition を残す。

Helper:

```bash
mozyo-bridge release check artifact
```

helper は `release check` family の read-only invariant を守るため repo の `dist/` を一切触らない。`python -m build --outdir <tmp>/dist` で隔離 tmp に書き出し、wheel / sdist を `<tmp>/extracted` に展開してから candidate scan と classifier を実行する。helper は safe-code pattern を内部で除外し、real credential literal / tracked secret file / personal path を strict-fail (exit 1) で返す。operator は残った blocker を clean に直すか active release ticket に disposition を残す。

### Canonical Renderer / Plugin Mirror Drift

`mozyo-bridge scaffold canonical [--check]` で render される router pair (`_router/AGENTS.md` / `_router/CLAUDE.md`) と governed preset workflow pair (`redmine-governed/agent-workflow.md` / `redmine-rails-governed/agent-workflow.md`) は canonical source (`src/mozyo_bridge/scaffold/canonical_sources/*.yaml`) から再生成可能であること、`plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/` は `skills/mozyo-bridge-agent/` の byte mirror であることを release 前に確認する。

手元検査:

```bash
mozyo-bridge scaffold canonical --check --repo .
scripts/sync_plugin_skill.sh --check
```

Helper:

```bash
mozyo-bridge release check drift
```

両 sub-check を 1 command で走らせる release helper。clean tree で exit 0、いずれか drift があれば strict-fail (exit 1) として `result: blocker` を返す。helper は `release check` family の read-only / idempotent invariant を守り、worktree や mirror に書き戻さない。復旧 command は stdout に verbatim で列挙される (`mozyo-bridge scaffold canonical` / `scripts/sync_plugin_skill.sh`, no `--check`)。

CI gate:

- `.github/workflows/test.yml` の `python -m unittest discover -s tests -v` step が、`tests/test_docs_canonical_workspace.py` の `CanonicalRendererTest` / `GovernedWorkflowCanonicalTest` (canonical render) と `tests/test_plugin_marketplace.py` の `PluginMarketplaceTest` (mirror byte gate + `sync_plugin_skill.sh --check` shell gate) を毎 push / PR で実行する。release helper を pre-merge gate として別に追加せず、unittest layer に集約する。
- release helper `release check drift` は pre-release operator が release commit 直前に 1 command で確認するための facade。active release ticket の audit trail にも `release check drift` の出力を貼る運用とする。

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
Asana / Redmine / none の scaffold、`scaffold status`、`doctor --target` を active release ticket に記録する。

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

- TestPyPI / installed dogfood は各 feature lane へ直列結合せず、**専用の release issue へ集約する** (Redmine #13967)。feature lane は early hibernate し **dogfood の execution/evidence を release issue へ durable に委譲する (source issue の close authority と owner close approval は委譲せず coordinator の通常経路に残る)**。配布正本は skill `references/release.md` `## Release-dogfood の集約`、repo-local wiring は `vibes/docs/logics/coordinator-sublane-development-flow.md` `## Early hibernate / dogfood 集約 / drain-queue / late-finding escalation`。
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

- This section defines the **package release version** written to package metadata,
  Git tags, and release notes. It is separate from Redmine Version objects, which
  are roadmap / milestone / acceptance grouping surfaces and must not be treated
  as either the package version source of truth or the active lane-set authority.
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
  - Helper: `mozyo-bridge release publish --testpypi --source-sha <40-hex> --expected-version 0.1.0a1 --source-ref refs/heads/<branch>` は exact-candidate dispatch を行う (Redmine #13601)。`--source-ref` は **origin 上の ref literal** で綴る (`refs/heads/<branch>` が canonical、短縮 `<branch>` も可)。local remote-tracking 表記 (`origin/<branch>` / `refs/remotes/origin/<branch>`) は **曖昧なため** helper が dispatch 前に exact な訂正付きで reject する (remote が同名 branch を持ちうるので推測しない。「origin 上に存在しないから」ではない)。exactly-one は構造保証ではなく動的検査であり、`ls-remote` の tail 一致は full path にも作用する (Redmine #13883、policy 正本: `release-helper-contract.md` の `### source_ref Spelling Policy` / `### exactly-one は構造保証ではなく動的検査`)。dispatch 前 client preflight が origin 上で non-peel ちょうど 1 件 + tip == source_sha を確認し、zero / multi / mismatch では dispatch を 0 回にする。workflow の event ref は `main` 固定で、exact `source_sha` / `expected_version` / `source_ref` / `dispatch_nonce` を input として渡す。trusted な build job が HEAD == source_sha / source_ref lineage / version mirror == expected_version / 同 SHA の `Test` success / version 未使用を fail-closed 照合し、build job と OIDC publish job を分離する。helper は run-name 中の nonce で run を決定的に相関し (exact 1 件以外は fail-closed)、run-id を active release ticket に貼れる shape で stdout に出す。polling は `mozyo-bridge release workflow wait --run-id <id> --timeout <seconds>` に明示的に委ねる。
- 検証は `pipx install --backend pip --index-url https://test.pypi.org/simple/ --pip-args "--extra-index-url https://pypi.org/simple/" mozyo-bridge==0.1.0a1` で行い、続けて `README.md` の `Beta Tester Install (GitHub main)` 節の acceptance smoke (rules install → skill install → `mozyo-bridge doctor` → isolated target に対する Asana / Redmine scaffold + doctor) を TestPyPI install に対して実行する。
- `pipx` が default backend に `uv` を使う環境では、TestPyPI の `--index-url` と dependency 用 `--extra-index-url` の組み合わせが期待通り解決されないことがあるため、TestPyPI 検証では `--backend pip` を明示する。
- 必要なら `git tag -a v0.1.0a1 -m "Pre-release v0.1.0a1"` で tag を打って push する。GitHub Release は作らない。

`mozyo-bridge release publish --plan` は現在の git ref / pyproject version / 最新 `Test` workflow conclusion / TestPyPI 既存 version の有無を at-a-glance で集約する。release を進めるか / pre-release に留めるかの判定は引き続き operator が行う (helper は GA / beta / patch を判定しない)。

### Internal beta の非循環 gate (exact candidate, Redmine #13601)

内部 beta の TestPyPI 手動配布は `origin/main` promotion や Redmine Version close を **先に要求しない**。publication checkpoint doctrine (`skills/mozyo-bridge-agent/references/workflow.md` の `## Publication checkpoint` / `### release gate は publication とは別である`) は Redmine Version close 前の `origin/main` push を禁じるが、内部 beta を publish するために公開履歴を先に進める必要が生じると `Version close → origin/main → TestPyPI → #13528/#13527 → Version close` の循環になる。これを壊すため、exact reviewed integration head を main 固定の workflow から直接 publish する。

- workflow 定義 / event ref は `main` 固定。exact `source_sha` を artifact authority、`expected_version` を照合対象、`source_ref` を origin lineage evidence として渡す。任意 staging ref を workflow authority にしない。
- trusted build job が fail-closed 照合 (HEAD == source_sha / source_ref が origin 上 exact 1 件の named ref でその tip == source_sha / 2-file version mirror == expected_version / candidate test.yml が trusted origin/main の test.yml と byte 一致 (#13601 j#76006 F1) / 同 SHA の `Test` (`test.yml`) success / expected_version が TestPyPI 未使用、`releases` schema 不成立や lookup 不能は fail-closed) を実行し、build job と OIDC publish job を分離する (`id-token: write` + `environment: testpypi` は publish job のみ)。
- 順序: #13528「TestPyPI publish」→ #13527「exact install QA」。install QA は publish 済み exact version に対して `scripts/install_testpypi_dev.sh <exact version>` で行い、`origin/main` promotion や Version close を前提条件にしない。これにより upstream blocker を全解消しても internal beta 直前で停止する循環がなくなる。
- automatic main-CI dev publish path は後方互換に維持する。owner 承認済みの `testpypi` required reviewer 導入後は automatic path も deployment approval 待ちになる (意図的な OIDC protection 優先)。
- 外部 environment 変更 (required reviewer / main-only deployment branch policy) と実際の dispatch は implementation review green 後の owner action として分離する。この doc / helper 変更自体は environment を変更せず、publish もしない。

### Patch Release

1. fix を main に merge し `Test` を pass させる。
   - Helper: `mozyo-bridge release workflow wait --run-id <id> --timeout <seconds>` で polling できる。
2. Version Bump 手順で `Z` を 1 上げて単独 commit する (`mozyo-bridge release bump --to X.Y.Z` → `git commit -m "Release vX.Y.Z"` → `git push`)。
3. 必要なら `Publish to TestPyPI` workflow で TestPyPI rehearsal を行う (`mozyo-bridge release publish --testpypi --source-sha <40-hex> --expected-version X.Y.Z --source-ref refs/heads/<branch>`, Redmine #13601)。`--source-ref` は origin 上の ref literal で綴る (Redmine #13883)。
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
