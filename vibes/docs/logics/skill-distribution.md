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
- Codex install は `scripts/install_codex_skill.sh` で public GitHub repository `hollySizzle/mozyo_bridge` の `skills/mozyo-bridge-agent` から `${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/` へ同期する。
- Claude install は `scripts/install_claude_skill.sh` で public GitHub repository `hollySizzle/mozyo_bridge` の `.claude/skills/mozyo-bridge-agent` と `skills/mozyo-bridge-agent` を対象 project の `.claude/skills/mozyo-bridge-agent/` と `skills/mozyo-bridge-agent/` へ同期する。
- install source は必要に応じて `MOZYO_BRIDGE_SKILL_REPO` (`owner/repo`), `MOZYO_BRIDGE_SKILL_REF`, `MOZYO_BRIDGE_SKILL_PATH` で上書きできる。
- Claude install の対象 project は `MOZYO_BRIDGE_CLAUDE_PROJECT_DIR` で上書きできる。
- root `AGENTS.md` / `CLAUDE.md` は skill と docs への router のままにする。

## Install Commands

Codex skill:

```bash
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_codex_skill.sh | sh
```

Claude Code project skill:

```bash
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_claude_skill.sh \
  -o /tmp/install_mozyo_bridge_claude_skill.sh
MOZYO_BRIDGE_CLAUDE_PROJECT_DIR=/path/to/project \
  sh /tmp/install_mozyo_bridge_claude_skill.sh
```

Claude Code は対象 project root から起動する。`.claude/skills/` は project-local であり、Codex の `${CODEX_HOME:-$HOME/.codex}/skills/` とは install scope が違う。

## README との役割分担

- README は public user 向けの install / command / safety summary に留める。
- skill 配布の配置理由、override env、禁止事項はこの文書を正本にする。
- skill 本体の runtime reference は `skills/mozyo-bridge-agent/references/` に置き、README や root router へ詳細規約を重複させない。

## 禁止事項

- skill ディレクトリに README や install guide を増やさない。
- Claude 専用 frontmatter を共通 `SKILL.md` に混ぜない。
- Codex 専用 metadata を Claude adapter に混ぜない。
- secret や local `.env` を skill 配布対象に含めない。
- Codex の標準 install path を local checkout 依存にしない。
