# Pre-commit focused verification (opt-in hook)

Redmine #13079 (親 US #13074、triage decision #13074 j#71212 child 2)。

`scripts/pre-commit-focused.sh` は、commit のたびに **秒単位で終わる staged 検証**
だけを回す opt-in の git pre-commit hook である。#12752 / #13078 の
module-to-test impact resolver (`mozyo-bridge tests resolve`) を運用に接続する
最初の自動化面で、full suite の実行タイミングは変えない。

## 何を回すか

1. `git diff --cached --check` — whitespace / conflict marker。
2. `mozyo-bridge docs audit-impact --staged --check-generated` — staged の
   docs / catalog impact (docs catalog を持たない repo では skip)。
3. `mozyo-bridge tests resolve --staged --format targets` — staged path から
   focused tests を解決してその場で実行 (#13078 の分類精緻化により、典型的な
   governed diff — src + tests + `vibes/docs/**` — は focused selection に乗る)。

実測の目安 (mozyo_bridge repo、2026-07-03): resolver ~0.3 秒、focused 選択の
実行は数百 test / 1 秒未満。full suite (~4400 tests / ~35 秒) はここでは回らない。

## 境界 (この hook が「やらない」こと)

- **opt-in のみ。** 自動 install / mandatory 化はしない。scaffold も CI もこの
  hook を配布・強制しない。採用は operator の per-repo 判断である。
- **full suite を hook で回さない。** resolver が fail-closed に `full` を
  推奨した場合、hook は full を実行せず、実行すべき command を表示して commit を
  通す。理由: 数十秒かかる pre-commit hook は `--no-verify` 回避の常態化を招き、
  「通った気になる」形骸化した gate は無い方がましである。full suite は
  pre-push / CI (quick/full lane、Redmine #12753) の責務のまま。
- **governed Required Verification ではない。** 本 hook は利便レイヤであり、
  governed preset の Required Verification / implementation_done の検証記録義務を
  代替しない。規約側への接続 (resolver focused テストの明文化) は Redmine #13080
  が別 child として扱う。
- `git commit --no-verify` で常にバイパスできる (git 標準)。バイパスした commit の
  検証責務は通常どおり実装者の記録義務側に残る。

## Install / Uninstall / 手動実行

hook の実体は repo に committed な `scripts/pre-commit-focused.sh` であり、
`.git/hooks/` へは **symlink または copy を operator が置く** (repo は
`.git/hooks` を配布できないため、これが opt-in の実体である)。

```sh
# install (repo root で)
ln -s ../../scripts/pre-commit-focused.sh "$(git rev-parse --git-path hooks)/pre-commit"

# uninstall
rm "$(git rev-parse --git-path hooks)/pre-commit"

# hook を介さず手動実行 (staged がある状態で)
sh scripts/pre-commit-focused.sh
```

### worktree での注意

- `git rev-parse --git-path hooks` は **linked worktree でも共通 git dir の
  `hooks/` に解決される** (git の既定)。つまり main checkout で install すると、
  同じ repo から切った全 worktree (sublane worktree を含む) で hook が有効になる。
- worktree ごとに on/off を分けたい場合は `extensions.worktreeConfig` を有効化した
  うえで per-worktree の `core.hooksPath` を設定する (git 標準機能)。この doc は
  既定の共有挙動を正とし、per-worktree 分離は operator の任意設定とする。
- sublane worktree は main checkout と hooks を共有するため、**coordinator が
  main checkout で install すれば全実装 lane に効く**。lane 側での個別操作は不要。

## 実装 / 環境の詳細

- CLI 解決順: `MOZYO_BRIDGE_CMD` env → PATH 上の `mozyo-bridge` → script 隣接の
  `src/mozyo_bridge` (この repo の checkout 内なら PYTHONPATH fallback)。
- focused 実行の interpreter は `MOZYO_PYTHON` (default `python3`)。
- 失敗 (whitespace / docs impact / focused test の red) は非ゼロ exit で commit を
  block する。resolver の `full` 推奨と「選択 0 件」は block しない (上記境界)。
- script の挙動は `tests/integration/e_150_quality_architecture/f_150_ci_verification/test_pre_commit_hook.py`
  が fixture git repo 上で固定する。

## 参照

- resolver 本体: #12752 (`mozyo-bridge tests resolve`)、分類精緻化: #13078。
- CI quick/full lane: `vibes/docs/logics/ci-quick-full-lane-policy.md` (#12753)。
- test 配置 / discovery: `vibes/docs/logics/tests-placement-discovery-policy.md`。
- 規約接続 (Required Verification 明文化): #13080 (未着手の別 child)。
