# Skill Distribution Logic

## 方針

Claude / Codex 両対応は、共通 skill 本体と tool-specific adapter を分けて扱う。

- 共通本体: `skills/mozyo-bridge-agent/`
- Claude Code adapter: `.claude/skills/mozyo-bridge-agent/SKILL.md`
- Codex metadata: `skills/mozyo-bridge-agent/agents/openai.yaml`

## 理由

- Claude Code project skill は `.claude/skills/<skill>/SKILL.md` を入口にする。
- Codex skill は `SKILL.md` を中心に、必要に応じて `agents/openai.yaml`、`references/`、`scripts/`、`assets/` を持つ。
- 両者は `name` / `description` frontmatter と supporting files の考え方が近いが、配置と tool-specific metadata は同一ではない。

## 運用

- 共通 workflow は `skills/mozyo-bridge-agent/SKILL.md` に置く。
- 詳細は `skills/mozyo-bridge-agent/references/` に分離する。
- Claude 専用設定は `.claude/skills/mozyo-bridge-agent/SKILL.md` にだけ置く。
- Codex UI metadata は `skills/mozyo-bridge-agent/agents/openai.yaml` に置く。
- Codex local install は `scripts/install_codex_skill.sh` で `${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/` へ同期する。
- root `AGENTS.md` / `CLAUDE.md` は skill と docs への router のままにする。

## 禁止事項

- skill ディレクトリに README や install guide を増やさない。
- Claude 専用 frontmatter を共通 `SKILL.md` に混ぜない。
- Codex 専用 metadata を Claude adapter に混ぜない。
- secret や local `.env` を skill 配布対象に含めない。
