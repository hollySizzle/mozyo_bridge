# Skill Distribution Logic

## 方針

Claude / Codex 両対応は、共通 skill 本体と tool-specific adapter を分けて扱う。

- 共通本体: `skills/mozyo-bridge-agent/`
- Claude Code adapter: `.claude/skills/mozyo-bridge-agent/SKILL.md`
- Codex metadata: `skills/mozyo-bridge-agent/agents/openai.yaml`

## 理由

- Claude Code は project skill (`<project>/.claude/skills/<name>/SKILL.md`) と user/personal skill (`~/.claude/skills/<name>/SKILL.md`) の両方を officially 読み込む。precedence は Enterprise → Personal (`~/.claude/skills/`) → Project (`.claude/skills/`) → Plugin で、矢印の順で earlier が override する。同名 skill が複数 scope に存在する場合、公式 docs は次の通り定めている (verbatim):

  > "When skills share the same name across levels, enterprise overrides personal, and personal overrides project. Plugin skills use a `plugin-name:skill-name` namespace, so they cannot conflict with other levels."

  source: <https://code.claude.com/docs/en/skills> (`Where skills live` セクション)。
- つまり同名 skill では **personal (`~/.claude/skills/`) が project (`.claude/skills/`) より優先される**。多くの開発ツールは project が user を上書きする慣習だが、Claude Code の skill 解決はその逆である点に注意する。
- Plugin skills は `plugin-name:skill-name` で namespace 分離されるため、他 scope と衝突しない。
- Codex skill は `${CODEX_HOME:-$HOME/.codex}/skills/<name>/` に user-global で配置し、`SKILL.md` を中心に必要に応じて `agents/openai.yaml`, `references/`, `scripts/`, `assets/` を持つ。
- `name` / `description` frontmatter と supporting files の考え方は近いが、配置と tool-specific metadata は同一ではない。

## Claude install scope

`scripts/install_claude_skill.sh` は `MOZYO_BRIDGE_CLAUDE_SCOPE` で配布範囲を切り替える。値は `project` (default) または `global`。両方に配布したい場合は二度実行する。

- `project` (default): 対象 project の `.claude/skills/mozyo-bridge-agent/` (adapter) と `skills/mozyo-bridge-agent/` (shared body) に同期する。Claude Code を当該 project root から起動した時だけ Claude が認識する。既存利用者の挙動を保つため default に置く。**注意**: Claude Code の precedence rule により、同じ name (`mozyo-bridge-agent`) を持つ personal skill が `~/.claude/skills/` にもあると、project copy は shadow されて Claude は personal copy を読む。project scope は (a) personal install を持たない利用者、(b) repo に skill を commit して contributor 全員に配布したい場合、(c) 実行時に personal を一時的に外して project copy を使いたい場合に有効。
- `global`: shared body を `${MOZYO_BRIDGE_CLAUDE_HOME:-$HOME/.claude}/skills/mozyo-bridge-agent/` に同期する。Claude Code の personal skill として全 session で有効。Codex の `${CODEX_HOME:-$HOME/.codex}/skills/` と対称な配置で、personal install は同名 project skill を override する。global scope では adapter は生成しない (Claude Code は user skill 直下の `SKILL.md` を直接読むため)。

`scope=both` は提供しない。Claude Code の precedence rule で personal が project を上書きするため、両方同名で install すると project copy が常に shadow され混乱を生む。両方の destination を同時に持ちたい場合は、明確な意図のもとで `scope=project` と `scope=global` を順に実行する。

## 運用

- 共通 workflow は `skills/mozyo-bridge-agent/SKILL.md` に置く。
- 詳細は `skills/mozyo-bridge-agent/references/` に分離する。
- Claude 専用設定は `.claude/skills/mozyo-bridge-agent/SKILL.md` にだけ置く。
- Codex UI metadata は `skills/mozyo-bridge-agent/agents/openai.yaml` に置く。
- Codex install は `scripts/install_codex_skill.sh` で public GitHub repository `hollySizzle/mozyo_bridge` の `skills/mozyo-bridge-agent` から `${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/` へ同期する。
- Claude install は `scripts/install_claude_skill.sh` で同じ public repository から同期する。`MOZYO_BRIDGE_CLAUDE_SCOPE=project|global` で scope を選ぶ。
- install source は必要に応じて以下で上書きできる:
  - `MOZYO_BRIDGE_SKILL_REPO` (`owner/repo`)
  - `MOZYO_BRIDGE_SKILL_REF` (branch / tag / commit)
  - `MOZYO_BRIDGE_SKILL_PATH` (Codex skill source path)
  - `MOZYO_BRIDGE_SHARED_SKILL_PATH` / `MOZYO_BRIDGE_CLAUDE_ADAPTER_PATH` (Claude script だけ)
  - `MOZYO_BRIDGE_SKILL_ARCHIVE_URL` (どちらの script でも、`https://codeload.github.com/...` 以外の tarball URL を直接指定できる。`file:///...` を使えば未 push の local checkout から smoke / 手動配布できる)
- Claude install の対象 project は `MOZYO_BRIDGE_CLAUDE_PROJECT_DIR`、Claude home は `MOZYO_BRIDGE_CLAUDE_HOME` で上書きできる。
- root `AGENTS.md` / `CLAUDE.md` は skill と docs への router のままにする。

## Install Commands

Codex skill (user-global):

```bash
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_codex_skill.sh | sh
```

Claude Code project skill (default):

```bash
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_claude_skill.sh \
  -o /tmp/install_mozyo_bridge_claude_skill.sh
MOZYO_BRIDGE_CLAUDE_PROJECT_DIR=/path/to/project \
  sh /tmp/install_mozyo_bridge_claude_skill.sh
```

Claude Code user-global (personal) skill:

```bash
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_claude_skill.sh \
  -o /tmp/install_mozyo_bridge_claude_skill.sh
MOZYO_BRIDGE_CLAUDE_SCOPE=global \
  sh /tmp/install_mozyo_bridge_claude_skill.sh
```

Claude Code は user/personal skill だけで運用する場合、project root から起動する必要はない。project skill を併用する場合は対象 project root から起動する。**personal/user skill (`~/.claude/skills/`) は同名 project skill を override する**ため、project 固有の skill body を使いたい場合は (a) personal install を行わない、または (b) project skill の name を変えて衝突を避ける。

## Local checkout install

未 push の commit や fork ブランチから配布したい場合は、ローカルで tarball を作成し、`MOZYO_BRIDGE_SKILL_ARCHIVE_URL` に `file://` URL を渡す。`tar --transform` は GNU tar 専用で macOS の bsdtar では動作しないため、staging directory を使う portable な手順を使う。

```bash
src=/path/to/mozyo_bridge
out=/tmp/mozyo_bridge_local.tar.gz
stage=$(mktemp -d)
mkdir -p "$stage/mozyo_bridge-local"
cp -R "$src/skills" "$stage/mozyo_bridge-local/"
mkdir -p "$stage/mozyo_bridge-local/.claude"
cp -R "$src/.claude/skills" "$stage/mozyo_bridge-local/.claude/"
tar -czf "$out" -C "$stage" mozyo_bridge-local
rm -rf "$stage"
MOZYO_BRIDGE_SKILL_ARCHIVE_URL="file://$out" \
  sh "$src/scripts/install_codex_skill.sh"
```

この経路は smoke / dogfood 用であり、通常の標準 install path は GitHub `main` のままとする。

## README との役割分担

- README は public user 向けの install / command / safety summary に留める。
- skill 配布の配置理由、override env、scope、precedence、禁止事項はこの文書を正本にする。
- skill 本体の runtime reference は `skills/mozyo-bridge-agent/references/` に置き、README や root router へ詳細規約を重複させない。
- `skills/mozyo-bridge-agent/references/` は Codex install と Claude install のどちらにも同期される配布対象である。agent 実行時に従うべき運用境界は、まずこの runtime reference に置く。

## 禁止事項

- skill ディレクトリに README や install guide を増やさない。
- Claude 専用 frontmatter を共通 `SKILL.md` に混ぜない。
- Codex 専用 metadata を Claude adapter に混ぜない。
- Claude Code が officially サポートしていない skill path を「標準」として docs に書かない。
- Claude Code の precedence rule (personal overrides project) を逆方向で記述しない。同名 skill では personal が project に勝つ。
- secret や local `.env` を skill 配布対象に含めない。
- Codex の標準 install path を local checkout 依存にしない。
