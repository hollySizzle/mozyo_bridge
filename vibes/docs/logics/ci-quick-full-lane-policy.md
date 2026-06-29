# CI Quick / Full Unittest Lane Split

Redmine #12753 (parent US #12510 `140_DelegatedCoordinator・NestedHandoff`,
Version `モジュール分割・テスト影響範囲整備枠`)。CI を「通常 PR の高速
フィードバック」と「全件保証」に分割する設計正本。`mozyo-bridge tests resolve`
(#12752) の影響範囲解決と `mozyo-bridge tests profile` (#12754) の runtime 計測を、
`.github/workflows/test.yml` の二レーン構成として束ねる。

## 受け入れ条件との対応

| 受け入れ条件 | 実現 |
| --- | --- |
| quick lane: lint/docs check/affected tests を実行する | `quick` job が `health check` (lint 相当) + `docs validate` (docs check) + 解決済み affected test を実行する |
| full lane: full `unittest discover` を merge gate / nightly/release 相当に残す | `full` job が `tests profile` (= `unittest discover -s tests`) を Python matrix 全体で実行。push/merge・nightly schedule・manual dispatch がトリガ |
| full lane が落ちた場合の扱いを Redmine workflow / release gate と矛盾なく定義する | 下記「full lane failure handling」 |
| local command と CI command が同じ resolver を使う | quick lane は `mozyo-bridge tests resolve --base <merge target>` を使い、local の `tests resolve` と同じ pure resolver を共有する |

## レーン構成

トリガ別にどちらのレーンが走るかを `if` で分ける:

- `pull_request` → `quick` のみ。
- `push` (branch への merge を含む) / `schedule` (nightly) / `workflow_dispatch`
  → `full` のみ。

両レーンは排他。PR では full を走らせず速度を取り、merge 以降・nightly で全件を担保する。

### quick lane (高速フィードバック)

single Python (3.12) で以下を順に実行する:

1. `mozyo-bridge health check` — module-health gate (lint/`too-many-lines` 相当, #12321)。
2. `mozyo-bridge docs validate --repo .` — governed catalog / 生成物の整合 (docs check)。
3. affected tests:
   ```bash
   git fetch --no-tags origin "$BASE_REF:refs/remotes/origin/$BASE_REF"
   mozyo-bridge tests resolve --base "origin/$BASE_REF" --format targets \
     | xargs python -m unittest
   ```
   `--base` は `git diff <REF>...HEAD` (merge-base diff) で PR 差分を取り、#12752 の
   pure resolver に渡す。`--format targets` は選択テストファイル、または
   fail-closed な `full` 推奨のとき `discover -s tests` を出力するので、未対応の
   変更は **全件にフォールバック** する (silent な空集合 = fail-open を出さない)。

`checkout` は `fetch-depth: 0`。merge target branch を ref として fetch してから
three-dot diff する (PR が *追加* した差分のみ。merge target に後から入った無関係な
commit は含めない)。

### full lane (全件保証)

`["3.10","3.11","3.12","3.13"]` matrix で以下を実行する:

1. `mozyo-bridge health check`。
2. `mozyo-bridge tests profile --slowest 20` — `unittest discover -s tests` と同一
   discovery を runtime summary 付きで実行 (#12754 default lane: quiet + summary,
   `--enforce` なしなので slow-test budget 違反は報告のみで fail させない)。
3. wheel build。

full lane が現行 CI の全件 gate をそのまま引き継ぐ。

## local / CI の resolver 共有 (受け入れ条件 #4)

quick lane と local は同じ `mozyo-bridge tests resolve` を呼ぶ。差は changed-path の
取得元だけ:

- local: working tree / index (`tests resolve` / `--staged` / `--all-changed`)。
- CI: merge target との merge-base diff (`tests resolve --base origin/<branch>`)。

両者とも `docs_tools/impact.py` の同じ filtering を通り、同じ pure
`resolve_impact_for_repo` に入る。`git_changed_paths_since` (CI) と
`git_changed_paths` (local) は changed-path 取得のみの違いで、選択ロジックは共有する。

手元で CI と同じ選択を再現するには:

```bash
mozyo-bridge tests resolve --base origin/main --format targets | xargs python -m unittest
```

## full lane failure handling (受け入れ条件 #3)

full lane は merge / nightly / release の安全 gate であり、その失敗は Redmine
workflow / release gate と整合する形で扱う:

- **full lane red は gate red**: full lane が落ちたら、その commit は全件保証を
  満たしていない。release / publish の前提を満たさないので、green な full lane なしに
  release・tag・publish を進めない (release gate の既存セマンティクスを継承)。
- **triage は Redmine 経由**: full lane の失敗は当該 work の Redmine issue で triage
  する。「実装変更が壊した失敗」は実装側で直す。
- **known-unrelated 失敗は記録して可視化する、黙殺しない**: 実装と無関係な
  既知の失敗 (例: 環境依存の cloud-drive 系) は、当該 issue の journal に
  「pre-existing / unrelated」である根拠 (再現条件・該当テスト) を残す。CI 側で
  silently skip したり threshold を緩めて隠したりしない。residual handling は
  durable record (Redmine journal) に置く。
- **quick lane green は full lane を代替しない**: quick は affected subset のみ。
  merge gate / release gate の権威は full lane が持つ。quick が green でも full が
  red なら gate は red。

これにより「速い PR フィードバック」と「全件保証」を分離しつつ、release gate の
権威 (= full discover green) を緩めない。

## 非目標

- test runner の差し替え (CI authority は `unittest discover` のまま;
  `tests-placement-discovery-policy` / `test-runtime-profiling-policy` を継承)。
- slow-test budget enforcement の CI default 有効化 (#12754 のまま opt-in)。
- release / publish ワークフロー自体の変更 (本 issue は CI test レーン分割のみ。
  release/tag/publish は対象外)。
- #12855 workflow fill decision surface の変更 (disjoint。本 issue は触れない)。
