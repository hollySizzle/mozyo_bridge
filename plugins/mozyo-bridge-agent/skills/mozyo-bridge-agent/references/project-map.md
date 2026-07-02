# Project map リファレンス

agent が作業対象の repository について project map — 重要 path、documentation namespace、ticket システムの binding — をどう解決するか。map 自体は repo 固有の data であり、この配布本文ではなく採用 repo 側にある。

## 採用 repo 自身の map を解決する

- 採用 repo 自身の project map をその local docs namespace から読む。governed-scaffold repo では docs catalog が入口である (`mozyo-bridge docs resolve <path>` が、触っている path に対して登録済みの map / spec docs を提示する)。
- 有用な project map は次を網羅する: package / import 名、repository と workspace root、repo の scaffold preset が選択した ticket システムと project id、実装 / test / docs の path、CI workflow、packaging metadata。
- root の `AGENTS.md` / `CLAUDE.md` は thin router のままとする。map の本文は repo の local docs に属し、本 reference はそれを複製しない。

## mozyo_bridge (この repository) — worked example

以下の具体的な map は `mozyo_bridge` repository 自身 — mozyo-bridge を開発する repo — のものであり、worked example として、また dogfooding session のためにここに保持する。採用 project は自身の同等物を自身の local docs で維持する。これらの値を copy しない。

### Repository

- Project: `mozyo-bridge`
- Import package: `mozyo_bridge`
- Package 名: `mozyo-bridge`
- Repository: https://github.com/hollySizzle/mozyo_bridge
- Workspace: repository の root
- `mozyo_bridge` の ticket システム: Redmine project `giken-3800-mozyo-bridge` (preset `redmine-governed`)。durable な作業記録は Redmine issue / journal である。
- Asana project: user ごとまたは private workspace で設定する (central preset が `asana` の採用 repo が使うもので、`mozyo_bridge` 自身は使わない)。

### 重要 path

- `src/mozyo_bridge/`: package 実装
- `tests/`: unit test
- `smoke/real_tmux_notify_smoke.py`: 実 tmux 通知 smoke test
- `.github/workflows/test.yml`: CI test workflow
- `.github/workflows/testpypi.yml`: TestPyPI publish workflow
- `.github/workflows/publish.yml`: production PyPI publish workflow
- `pyproject.toml`: package metadata
- `README.md`: user 向けの使用方法と安全上の注意
- `.env.example`: secret を含まない local 環境の example

### ドキュメント

- `vibes/docs/`: project documentation の namespace であり、runtime namespace ではない。
- `skills/mozyo-bridge-agent/`: Claude/Codex workflow guidance の共有 skill source。
- `.claude/skills/mozyo-bridge-agent/`: Claude Code の project-skill adapter。
