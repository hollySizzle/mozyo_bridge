# 外部 project への herdr backend 導入手順 (operator 向け)

mozyo_bridge 以外の project に herdr backend の運用 (coordinator/auditor pair + sublane) を導入する replay 可能な手順 (Redmine #13379)。**全 command は installed CLI (pipx 等の `mozyo-bridge` / `mozyo`) のみで完結する** — mozyo_bridge repo checkout や repo-local CLI (`PYTHONPATH=src python3 -m mozyo_bridge`) は前提にしない。lane 実運用 (作成 / dispatch / retire / 統合) の正本は `vibes/docs/tasks/herdr-lane-operations.md`、identity 設計の正本は `vibes/docs/specs/herdr-native-identity.md`。

## 前提

- installed CLI が herdr lane 世代 (2026-07-08 の #13377/#13380 統合以降相当) であること。版確認は `mozyo-bridge --version` と feature probe (`mozyo-bridge sublane create --help` に `--gateway-ready-timeout`、`mozyo-bridge handoff send --help` に `--target-lane` があること) で行う。version 文字列単独を evidence にしない (runtime fingerprint 規律)。
- CLI / agent CLI (claude / codex) の global update は **全 lane 静止時** に行う (#13378: 稼働中の idle pre-session agent は global update で pane ごと消える)。
- herdr client / server が導入済みであること (`command -v herdr`)。手動起動の shell で `MOZYO_HERDR_BINARY` が未設定なら `MOZYO_HERDR_BINARY=$(command -v herdr)` を inline 付与する (launch 注入済み agent には不要)。
- 対象 project の ticket システム (Redmine / Asana / none) が決まっていること。preset 選択に使う。

## 導入手順

1. **scaffold apply** — project root に router (`AGENTS.md` / `CLAUDE.md`) と `.mozyo-bridge/` 一式を配置する:

   ```sh
   mozyo-bridge scaffold apply <preset> --target <project_root> --backup
   ```

   - `<preset>` は project の ticket システムに合わせる (`redmine` / `redmine-governed` / `asana` / `none` 等。一覧は `mozyo-bridge scaffold apply --help`)。
   - 生成される `.mozyo-bridge/scaffold.json` が **mozyo 採用 marker** になる。未採用 directory での bare `mozyo` は fail-closed する (#13379。下記「未採用 directory の挙動」)。

2. **rules install** — preset rules store を配置する:

   ```sh
   mozyo-bridge rules install
   ```

   - home store (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/...`) に配置される。Dev Container / ephemeral home では `mozyo-bridge rules install --repo-local <project_root>` + `scaffold apply --repo-local` を使う。

3. **backend 選択** — `<project_root>/.mozyo-bridge/config.yaml` を作成 (既存なら追記) して herdr backend を宣言する。最小形:

   ```yaml
   version: 1
   terminal_transport:
     backend: herdr
   ```

   - config が無い / `backend: tmux` の project は従来どおり tmux cockpit のまま (byte-invariant)。壊れた config は entrypoint で fail-closed する。

4. **起動確認** — tmux の外の terminal で project root に cd して:

   ```sh
   mozyo --json    # herdr session-start のみ (attach しない)。"ready": true を確認
   mozyo           # session-start + herdr UI attach
   ```

   - `mozyo --json` の payload は `backend: herdr` / per-slot (claude / codex) の outcome + locator を返す。`--cc` / `--session` は tmux 専用 flag として明示拒否される。
   - tmux の中から実行すると attach は fail-closed する (`--no-attach` / `--json` は可)。

5. **lane 運用へ** — 以降の sublane 作成 / dispatch / retire / 統合は `vibes/docs/tasks/herdr-lane-operations.md` の標準形に従う。全 command は installed CLI で完結する。

## 未採用 directory の挙動 (#13379 gate)

- bare `mozyo` は、解決された repo root に採用 marker (`.mozyo-bridge/config.yaml` / `scaffold.json` / `workspace-anchor.json` / `workspace.json`) が無い場合、**agent session を一切起動せず** scaffold 導線を案内して停止する (exit 2)。導入前の directory で誤って叩いても実 agent は起動しない。
- **home directory は marker が有っても常に拒否**する。未採用 dir からの repo 解決は home の偶発 marker (`~/.tmux.conf` 等) まで遡りやすく、home 直下への迷子 session 生成 (#13379 j#73667 の観測) を構造的に塞ぐため。
- 採用 marker は明示的な採用操作 (`scaffold apply` / `workspace register` / config 作成) でのみ生まれる。`.mozyo-bridge/` directory の存在だけでは採用と見なさない (tooling の副生成があり得るため)。
- 明示 subcommand (`mozyo-bridge herdr session-start` / `mozyo layout apply cockpit` 等) はこの gate の対象外。

## 初回導入 smoke checklist (対象 project での実測)

初回導入時は以下を実測し、evidence を対象 project の ticket 側 journal に残す (private path / 実 project 名は mozyo_bridge 側の記録に書かない):

1. `mozyo --json` → `ready: true` (coordinator/auditor 両 slot が adopted/launched)。
2. `mozyo` attach → herdr UI に project workspace + claude/codex slot が見える。
3. lane 1 本の往復: `sublane create --execute` → dispatch → worker callback → `sublane retire` (手順は herdr-lane-operations.md)。
4. cross-project 干渉なし: 既存 project の workspace / lane が herdr UI・`mozyo-bridge status`・cockpit grouping に混線しないこと。registry identity (`mozyo-bridge agents targets`) が project ごとに分離していること。

## 記録の衛生

- journal / commit / 本 doc に host-local 絶対 path・実 consumer 名・secret-shaped value を書かない (正本: `vibes/docs/rules/public-private-boundary.md`)。
