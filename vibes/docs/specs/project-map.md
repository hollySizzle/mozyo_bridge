# Project Map

## 概要

- Project: `mozyo-bridge`
- Import package: `mozyo_bridge`
- Package name: `mozyo-bridge`
- Workspace: repository root
- GitHub repository: https://github.com/hollySizzle/mozyo_bridge
- Asana project: configure per user or private workspace

## Source of Truth

- 実行キュー: 利用者自身の Asana project
- グローバル規約・知識: 利用者自身の Notion workspace
- code と release artifact: この repository
- package metadata: `pyproject.toml`
- user-facing usage / safety: `README.md`
- CI / publish: `.github/workflows/`
- real tmux smoke test: `smoke/real_tmux_notify_smoke.py`

## 主要ファイル

- `src/mozyo_bridge/`: package implementation
- `tests/`: unit tests
- `smoke/real_tmux_notify_smoke.py`: 実 tmux notification smoke test
- `.github/workflows/test.yml`: test workflow
- `.github/workflows/testpypi.yml`: TestPyPI publish workflow
- `.github/workflows/publish.yml`: production PyPI publish workflow
- `.env.example`: local env の例。secret は入れない。

## Documentation Namespace

`vibes/docs/` は documentation namespace である。runtime path ではない。

- `vibes/docs/rules/`: 作業規約
- `vibes/docs/specs/`: project 構造・仕様
- `vibes/docs/logics/`: 判断 logic・release flow
- `vibes/docs/temps/`: template
