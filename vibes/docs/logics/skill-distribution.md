# Skill Distribution Logic

## 方針

Claude / Codex 両対応は、共通 skill 本体と tool-specific adapter / packaging を分けて扱う。

- 共通本体 (canonical): `skills/mozyo-bridge-agent/`
- Claude Code project adapter: `.claude/skills/mozyo-bridge-agent/SKILL.md`
- Codex metadata: `skills/mozyo-bridge-agent/agents/openai.yaml`
- Claude plugin marketplace packaging: `.claude-plugin/marketplace.json` (repo root) と `plugins/mozyo-bridge-agent/.claude-plugin/plugin.json` + `plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/` (shared body の mirror)

## 配布経路

Claude Code 用の primary install は plugin marketplace 経由とし、Codex は canonical GitHub skill path に対する `$skill-installer`、CLI / rules は pipx + `mozyo-bridge rules install` を使う。curl/script による install は **legacy fallback** であり、新規 install では推奨しない。

- Claude Code (primary): `claude plugin marketplace add hollySizzle/mozyo_bridge` → `claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user`
- Codex (primary): `$skill-installer` に canonical path `https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent` を渡す
- CLI / rules: `pipx install mozyo-bridge` + `mozyo-bridge rules install`
- Legacy fallback (deprecated for new installs): `scripts/install_codex_skill.sh`, `scripts/install_claude_skill.sh`

## Legacy Project Claude Skill (`.claude/skills/mozyo-bridge-agent/`) Grace-Period Deprecation

repo にコミット済みの `.claude/skills/mozyo-bridge-agent/` 配下 (`SKILL.md` adapter stub + `references/safety.md` partial mirror; `git ls-files .claude/skills/` で 2 file 確認) は、`MOZYO_BRIDGE_CLAUDE_SCOPE=project` での legacy install / project root から起動した Claude Code が直接 load する経路を support する。これを **grace-period deprecate** に置き、即時 `git rm` 削除は行わない (Asana audit `1214732699548536` / `1214733817990357`)。

選定理由 (keep / remove / grace-period deprecate のうち grace-period deprecate を選定):

- 直近 commit `802a88243` (Asana task `1214779823377861`) が project-scope mirror に `references/safety.md` を意図的に追加したばかりで、その意図 (project root から起動する Claude Code セッションで shared skill body の partial mirror を提供する) を 1 release 以内に逆転させると churn が発生する。
- plugin marketplace path (`mozyo-bridge-agent:mozyo-bridge-agent` namespace) が新規 install の primary であることは確定 (`scripts/install_claude_skill.sh` 自体は `1214733632421625` で deprecation 通知済み) だが、既存の project-scope flow を中断する hard remove は audit recommendation R3 の "medium priority" の射程外。
- `keep` を選ばない理由: project skill と personal skill (`~/.claude/skills/`) の precedence gotcha は `1214732699548536` で audit 済み。plugin namespace が長期的に正しい解で、project-scope は段階的に縮小すべきという結論は audit verdict と整合する。
- `remove` を選ばない理由: 直近 commit を即時打ち消す churn + 既存 fallback 利用者の挙動変化 + tests/scaffold が project-scope install を前提とする箇所の同時改変が必要で、本 task の単発スコープを超える。

Grace period 中の運用:

- `.claude/skills/mozyo-bridge-agent/` 配下の tracked file (SKILL.md, references/safety.md) は当面残す。canonical 本体は `skills/mozyo-bridge-agent/` のまま。
- 新規 install では plugin marketplace 経由を推奨し、project-scope install は legacy fallback として扱う (本文書の `## Legacy Global Claude Skill Deprecation` 節と同じ位置づけ)。
- `scripts/install_claude_skill.sh` の `MOZYO_BRIDGE_CLAUDE_SCOPE=project` 動作は **当面残置** する。`MOZYO_BRIDGE_CLAUDE_SCOPE=global` と同様、deprecation 通知 (script header の DEPRECATED block) で警告する。即時の warn-and-exit / 失敗化は行わない。
- `references/safety.md` 等の project-scope mirror が canonical (`skills/mozyo-bridge-agent/references/safety.md`) からドリフトしないよう、mirror は手動更新時に canonical を先に編集し、その content を写す運用とする (`PluginMarketplaceTest` が plugin mirror のドリフトを検出するのと同じ思想)。project-scope mirror については現時点で自動 drift test は無いが、本節の grace-period が終わるまでに doc-regression test を追加するか撤去するかを判断する。

Removal criteria (本 grace period を解除する条件):

- 次の release line で project-scope install を相談される事例が無くなったとき。
- かつ `scripts/install_claude_skill.sh` の caller / smoke で `MOZYO_BRIDGE_CLAUDE_SCOPE=project` を使う経路が無いことが確認できたとき (`grep -rn "MOZYO_BRIDGE_CLAUDE_SCOPE=project"` がない、または fresh-tester acceptance smoke でも plugin path のみ使用に切り替わったとき)。
- 上記が満たされたら、別 task として `.claude/skills/mozyo-bridge-agent/` の `git rm` + scaffold/test/doc の整合更新を実施する。本 task ではその follow-up を Open task として残す。

## Legacy Global Claude Skill Deprecation

`~/.claude/skills/mozyo-bridge-agent/` 配下に `scripts/install_claude_skill.sh` で配置する Claude personal skill (legacy global Claude skill) は、**新規 install において deprecated** とする (Asana audit `1214732699548536` / `1214733632421625`)。新規 install は Claude plugin marketplace 経由 (`claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user`) のみを推奨する。

- 既存 install の取り扱い: 既に `~/.claude/skills/mozyo-bridge-agent/` を持つ user の home directory を、本 repository から自動削除しない / 強制 cleanup しない。cleanup の判断は user に委ねる。`mozyo-bridge doctor` は legacy directory を引き続き scan するが、`claude_skill: plugin-managed` (plugin が検出された場合) を期待状態として扱う。
- `scripts/install_claude_skill.sh` の存続条件: 以下のいずれかに当てはまる環境のためにのみ残す: (a) plugin marketplace を使えないオフライン環境、(b) 内部 mirror / 内部 fork からの install、(c) fresh-tester acceptance smoke の検証。これらの条件に当てはまらない通常 install は、本 script を使わず plugin marketplace 経由で行う。
- 配置 precedence の落とし穴: 同名 skill では personal (`~/.claude/skills/`) が project (`.claude/skills/`) を override する。新規 install が plugin marketplace path のみを使えば、`mozyo-bridge-agent:mozyo-bridge-agent` の namespace 分離が効くため、この precedence gotcha を踏まない。legacy global Claude skill を残したまま plugin path と共存させると、plugin install 後も personal copy が残り続け、user が後で contents drift を気にする必要が出る (本節が deprecated として推奨しない理由)。
- 廃止 timeline: hard removal は本 task の scope 外。`scripts/install_claude_skill.sh` の即時削除 / 即時 break-only stub 化は禁止 (deprecation 通知 + 推奨経路の切替に留める)。実際の install / cleanup 動作の変更は別 Asana task で取り扱う。

## Source of Truth と drift 対策

canonical な skill 本体は `skills/mozyo-bridge-agent/` に置き、Claude plugin marketplace が配布する `plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/` はその mirror として扱う。

- canonical を変更したら必ず `scripts/sync_plugin_skill.sh` を実行して mirror を更新する。
- CI / pre-commit gate には `scripts/sync_plugin_skill.sh --check` を使う。`--check` は dry-run (rsync `-an --delete --itemize-changes`) で、何も書き込まずに drift があれば exit 1 を返し、recovery command (`scripts/sync_plugin_skill.sh`、no `--check`) を stderr で案内する。書き込みを伴わないので CI で worktree を汚さない。
- 両者の drift は `tests/test_mozyo_bridge.py::PluginMarketplaceTest` が二つの経路で検証する: (a) Python の sha256 walker による file list + content hash 比較 (`test_plugin_skill_mirror_matches_canonical`)、(b) `sync_plugin_skill.sh --check` の exit code と stderr の動作 pin (`test_sync_script_check_mode_*` 群)。両者が同じ drift を独立に検出する。
- workflow body の semantic drift (canonical と mirror を同期して両方から重要 section を抜き落とすケース) は `SkillCrossWorkspaceGuidanceTest` と `SkillWorkflowSemanticAnchorsTest` が pin する。前者は Redmine #10332 cross-workspace / `--mode standard` guidance を、後者は handoff lifecycle、role boundary、Codex direct-edit gate、Repo-Local Guardrail Autonomous Lane、audit-owned commit authority、workflow change verification の代表 phrase / section heading を verbatim で要求する。byte 一致だけでは捕まらない governance regression をここで止める。
- skill / plugin mirror に対して canonical renderer (`mozyo-bridge scaffold canonical [--check]`) は採用しない。両者は **pure byte mirror** であり、conditional rendering が必要な router pair / governed preset workflow と性質が異なるため、`sync_plugin_skill.sh` の rsync gate + 上記 test 群を正本 mechanism とする。
- plugin の install 時 Claude Code は plugin directory を cache にコピーするため、plugin root の外を参照する symlink (例: `../../../skills/mozyo-bridge-agent`) は使えない。docs: <https://code.claude.com/docs/en/plugins-reference#plugin-caching-and-file-resolution>
- mirror が手動編集された場合も drift test で落ちる。`plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/` を直接編集せず、canonical を編集してから sync する。

### Install Command Drift (Redmine #10699)

operator-facing install command snippet (`claude plugin marketplace add hollySizzle/mozyo_bridge` / `claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user` / `pipx install mozyo-bridge` / `mozyo-bridge rules install` / Codex `$skill-installer https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent`) は README.md / 本 file / `vibes/docs/logics/bootstrap.md` / `vibes/docs/logics/scaffold-rules.md` の複数箇所に verbatim で出現する。これは exact-string copy であり audience-specific variant ではないため、1 箇所だけ更新されると user が doc 間で異なる copy-paste recipe を得る drift 実害がある。

owner decision で README / ReleaseDocs 全体 canonical 化は対象外、また install 手順は user-facing readability を最重視するため canonical render / 共有 include は採用しない。代わりに最軽量機構として `tests/test_mozyo_bridge.py::InstallCommandConsistencyTest` が正本 install command 列を verbatim で pin し、`PINNED_INSTALL_COMMANDS` 表に列挙した各 command が列挙した各 doc に出現することを assert する。同 test は intentional な audience variant (`pipx install --force git+https://...` Beta Tester form) も pin し、PyPI 形式と git-main 形式が誤って同型化される regression も止める。

- 新規 doc に install command を追加する場合は `PINNED_INSTALL_COMMANDS` の paths tuple に追加する。
- 命令文字列を更新する (例: marketplace name 変更、scope flag 変更) 場合は同 test の command 文字列と全 doc を同 commit で更新する。
- 共有 include / canonical render / 新規 logic doc は **意図的に追加しない**。drift 検出は unit test 層に集約する (`SkillCrossWorkspaceGuidanceTest` / `SkillWorkflowSemanticAnchorsTest` precedent と同じ)。

## Marketplace / plugin metadata

- `.claude-plugin/marketplace.json` は `name`, `owner`, `plugins` を持ち、`mozyo-bridge-agent` plugin を `./plugins/mozyo-bridge-agent` の explicit path で参照する (marketplace root から resolve)。
- **Caveat (verified 2026-05-12, commit 542edad)**: `metadata.pluginRoot` は使わない。Claude plugin docs L181 (<https://code.claude.com/docs/en/plugin-marketplaces>) は `pluginRoot` が relative source path に prepend されると記載するが、現行 Claude CLI の (a) validator schema は source に `./` prefix を強制 (L233 "Must start with `./`") し、(b) installer は `./`-prefixed source を marketplace root から resolve する (`pluginRoot` を prepend しない)。結果として `pluginRoot: "./plugins"` + `source: "./mozyo-bridge-agent"` は `claude plugin validate .` を通っても GitHub marketplace 経由の install 時に `Source path does not exist` で失敗する。verification log: Asana 1214730609356621 comment 1214731507813769。
- `plugins/mozyo-bridge-agent/.claude-plugin/plugin.json` は `name`, `description`, `repository`, `license`, `keywords`, `author` を持つ。`version` は意図的に省略し、git commit SHA を version として使う (docs: <https://code.claude.com/docs/en/plugin-marketplaces#version-resolution-and-release-channels>)。これにより `pyproject.toml` の package version と plugin version を別管理できる。
- marketplace name (`mozyo-bridge`) は Anthropic 公式 reserved name (`anthropic-marketplace`, `claude-code-marketplace` など) と衝突しない kebab-case。

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

### Claude Code plugin marketplace (primary)

```bash
claude plugin marketplace add hollySizzle/mozyo_bridge
claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user
```

Plugin skills are namespaced `mozyo-bridge-agent:mozyo-bridge-agent`, so they do not conflict with personal (`~/.claude/skills/`) or project (`.claude/skills/`) skills with the same name. This is the recommended path because (a) it avoids the personal-overrides-project precedence gotcha, (b) it pins to a marketplace catalog the team controls, and (c) `/plugin marketplace update` refreshes content without per-user shell scripts.

### Codex `$skill-installer` (primary)

Canonical path: <https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent>

Codex `$skill-installer` reads `SKILL.md` and copies the surrounding directory tree into `${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/`. Use the canonical path above so the install gets `references/`, `agents/openai.yaml`, etc.

### CLI / rules

```bash
pipx install mozyo-bridge
mozyo-bridge rules install
mozyo-bridge rules status
```

### Fallback: curl-based scripts

```bash
# Codex skill (fallback, user-global)
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_codex_skill.sh | sh

# Claude Code skill (fallback, user-global)
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_claude_skill.sh \
  -o /tmp/install_mozyo_bridge_claude_skill.sh
MOZYO_BRIDGE_CLAUDE_SCOPE=global sh /tmp/install_mozyo_bridge_claude_skill.sh
```

`MOZYO_BRIDGE_CLAUDE_SCOPE=global curl ... | sh` の形は env が `curl` にしか渡らず、script は default の `scope=project` で走るため不可。

Claude Code は user/personal skill だけで運用する場合、project root から起動する必要はない。project skill を併用する場合は対象 project root から起動する。**personal/user skill (`~/.claude/skills/`) は同名 project skill を override する**ため、project 固有の skill body を使いたい場合は (a) personal install を行わない、(b) project skill の name を変えて衝突を避ける、または (c) plugin marketplace 経由で `mozyo-bridge-agent:mozyo-bridge-agent` namespace を使う。

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

## Beta Tester Verification

beta tester が GitHub `main` から CLI を install した後、skill 配布が期待通り動いていることを確認する流れ。詳細 README handoff は `README.md` の `Beta Tester Install (GitHub main)` 節を見る。本節はそこに重複させずに、検証観点と落とし穴だけを残す。

### Primary path verification (plugin marketplace + Codex $skill-installer)

1. Claude plugin marketplace install: `claude plugin marketplace list` に `mozyo-bridge` が出て、`claude plugin list` に `mozyo-bridge-agent@mozyo-bridge` が出ていることを確認する。plugin skill は Claude Code が `~/.claude/plugins/cache/` 配下に展開し、`mozyo-bridge-agent:mozyo-bridge-agent` namespace で読み込まれる。`mozyo-bridge doctor` の `claude_skill` section は legacy directory (`~/.claude/skills/` / `<project>/.claude/skills/`) に加えて plugin cache (`~/.claude/plugins/cache/mozyo-bridge/mozyo-bridge-agent/<sha>/skills/mozyo-bridge-agent/SKILL.md`) も検出する。primary path だけで install した場合は `claude_skill: plugin-managed` が出る。これは期待状態で、`next_action` は空 (legacy install hint は plugin が検出されている間は抑制される)、overall doctor 結果も `result["ok"] == True` を維持する。primary path の最終確認は `claude plugin list` で行う。
2. Codex user-global skill: `$skill-installer` を canonical path `https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent` で実行した後、`${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/SKILL.md` が存在し、`SKILL.md` の `name` / `description` が GitHub `main` の内容と一致する。`mozyo-bridge doctor` の `codex_skill: ok` でも同時に確認できる。
3. agent を再起動した後、Claude の skill 一覧に `mozyo-bridge-agent:mozyo-bridge-agent` (plugin) が、Codex の skill 一覧に `mozyo-bridge-agent` が出る。同 session 内では skill index がキャッシュされるため再起動を省略しない。

### Fallback path verification (curl/script + doctor)

curl/script による install を併用または primary 不可で fallback した場合の検証。

1. Codex: `scripts/install_codex_skill.sh` 実行後、`${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/SKILL.md` が存在する。
2. Claude: `curl ... -o /tmp/install_mozyo_bridge_claude_skill.sh` → `MOZYO_BRIDGE_CLAUDE_SCOPE=global sh /tmp/install_mozyo_bridge_claude_skill.sh` (env var は pipe の右側で `sh` の直前に置く) で `${MOZYO_BRIDGE_CLAUDE_HOME:-$HOME/.claude}/skills/mozyo-bridge-agent/SKILL.md` が legacy directory に作られる。`global` scope では `.claude/skills/` 配下に adapter を生成しない。`MOZYO_BRIDGE_CLAUDE_SCOPE=global curl ... | sh` の形は env が `curl` にしか渡らず、script は default の `scope=project` で走るため不可。
3. fallback path だけ使った場合、`mozyo-bridge doctor` の `codex_skill: ok` / `claude_skill: ok` で 1 と 2 を 1 command で確認できる。primary plugin install と fallback を併用すると plugin skill (`mozyo-bridge-agent:mozyo-bridge-agent`) と legacy skill (`mozyo-bridge-agent`) が両方有効になり、Claude Code 内で 2 つの skill 名として出る (plugin namespace で衝突しない)。

### PyPI release との見分け

- `mozyo-bridge --version` の出力は `pyproject.toml` の package version 文字列であり、GitHub `main` で未 bump の状態だと PyPI release と同じ string が表示されうる。skill 配布側で beta 適用を確認するには、(a) install 直後の plugin skill (`~/.claude/plugins/cache/...`) または legacy skill 内容と GitHub `main` の最新差分を突き合わせる、(b) 同 commit に紐づく未 release 変更 (例: 新 reference file 追加) の存在を確認する。

### Precedence の落とし穴

- Claude Code は同名 skill で personal (`~/.claude/skills/`) が project (`.claude/skills/`) を override する。fallback path で `MOZYO_BRIDGE_CLAUDE_SCOPE=global` を使い、かつ同 repo に project skill も置く構成は shadow を生む。plugin marketplace 経由 (primary path) で install した skill は `mozyo-bridge-agent:mozyo-bridge-agent` namespace に分離されるため personal / project と衝突せず、precedence の落とし穴を回避できる。
- Codex には Claude のような multi-scope precedence rule は documented されていない。Codex skill は user-global 配置のみを標準経路とする。

`scope=both` を提供しない理由、override env、archive URL の意味は本文書の `Claude install scope` / `運用` セクションを正本にする。

## 禁止事項

- skill ディレクトリに README や install guide を増やさない。
- Claude 専用 frontmatter を共通 `SKILL.md` に混ぜない。
- Codex 専用 metadata を Claude adapter に混ぜない。
- Claude Code が officially サポートしていない skill path を「標準」として docs に書かない。
- Claude Code の precedence rule (personal overrides project) を逆方向で記述しない。同名 skill では personal が project に勝つ。
- secret や local `.env` を skill 配布対象に含めない。
- Codex の標準 install path を local checkout 依存にしない。
- `plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/` を直接編集しない。canonical (`skills/mozyo-bridge-agent/`) を編集してから `scripts/sync_plugin_skill.sh` で mirror を再生成する。drift test (`PluginMarketplaceTest`) が手動編集を検出する。
- plugin root の外を参照する symlink (例: `../../../skills/...`) を plugin tree 内に作らない。plugin install 時に cache へコピーされ、symlink の参照先は失われる。
- skill 配布 directory 名は `mozyo-bridge-agent` 固定。`mozyo-bridge-agent.bak`、`mozyo-bridge-agent.tmp`、`mozyo-bridge-agent.bak-plugin-only-test` 等の改名 copy / backup copy / 重複名 directory を同階層 (`skills/`、`plugins/mozyo-bridge-agent/skills/`、`.claude/skills/`、`~/.claude/skills/`、`${CODEX_HOME:-$HOME/.codex}/skills/`、`~/.claude/plugins/cache/...` のいずれにおいても) に置かない。Claude Code の skill discovery は directory 名を skill 名として登録する (frontmatter `name` を見ない) ため、改名 copy が並ぶと別 skill として available-skills list に並び (例: `mozyo-bridge-agent.bak-plugin-only-test` が新規 skill として現れる)、operator 操作 / agent invocation 双方で混乱を生む。Asana 1214732699548536 comment 1214732662430548 の Side finding として観測済み。一時的な検証で別名 copy を作る場合は別 directory tree (例: `/tmp/mozyo_bridge_skill_test/`) に出して skill discovery scan の対象外に置く。
