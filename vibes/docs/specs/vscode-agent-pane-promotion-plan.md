# VS Code Agent Pane Promotion Plan

## Purpose

VS Code Agent Pane PoC (`experimental/vscode-agent-pane/`) の管理境界を定義する。PoC と本体機能の境界が曖昧なまま dual pane 実装へ進む状態を避け、experimental からの昇格 path と将来の submodule / 独立 repo 化条件を durable に記録する。

判断の durable record は Redmine #11523 / #11531 (Design Consultation #55651)。本 doc はその docs 化であり、判断を置き換えない。

## Decision

**当面は `mozyo_bridge` monorepo 内の正式 subproject として管理する。** 独立 repo / submodule 化は下記の昇格条件が成立するまで行わない。

- 正式配置 path: `packages/vscode-agent-pane/`
- `experimental/` は PoC・使い捨て検証専用に戻す。恒久機能を置かない。
- mozyo_bridge Python package には extension を含めない (現行 contract を維持)。

### 採用理由

- CLI / backend + VS Code extension の monorepo 構成は一般的であり、構成として不自然ではない。
- 初期開発では CLI contract 変更と extension 変更を同一 commit / 同一 CI で扱える。別 repo 運用より変更追跡と review の負荷が低い。
- Marketplace 公開、release cadence 分離、外部利用者、独立 secrets / CI が必要になった時点で分離する方が、先に分離して同期コストを払うより安い。

### `packages/` を選ぶ理由 (`apps/` との比較)

- `apps/` は deploy 可能な application 群の置き場という含意が強い。extension は単独 app というより mozyo-bridge CLI の client であり、将来 MCP server package 等の兄弟 subproject が増える場合も `packages/` の方が一般化する。
- 現時点で subproject は 1 つだが、`packages/<name>/` 規約は Node 系 monorepo tooling (npm workspaces 等) の default と互換であり、後から workspace 化する場合の移行が小さい。

## Monorepo Subproject Operation

昇格後の `packages/vscode-agent-pane/` の運用境界:

- **Ownership**: mozyo_bridge repo と同一 (owner / Redmine project `giken-3800-mozyo-bridge` / 同一 gate workflow)。
- **Release**: 当面 release 対象にしない。private extension のまま、Marketplace publish も `vsix` 配布も行わない。mozyo-bridge PyPI release とは独立であり、release notes / version bump に extension を混ぜない。
- **CI**: extension の compile + E2E smoke は本体 Python test job とは別 job として path filter (`packages/vscode-agent-pane/**`) で起動する。本体 CI の所要時間に extension 依存 (VS Code download 等) を混ぜない。CI 追加自体は migration issue (#11532) の scope で判断する。
- **依存管理**: `node_modules/`, `out/`, `.vscode-test/` 等の生成物は git 管理しない (現行 PoC と同じ)。
- **Contract**: `vibes/docs/specs/vscode-agent-pane-contract.md` の CLI boundary contract を維持する。extension が CLI surface 以外の内部 API に依存し始めたら境界違反として review で block する。

## Migration Plan: experimental → packages

実行は Redmine #11532、移行後検証は #11533 が担当する。本 doc は手順の正本であり、実行結果は各 issue の journal に残す。

1. **前提確認** — PoC 成功判定 (#11521 系の smoke / E2E green) が Redmine に記録されていること。未達なら移行しない。
2. **`git mv experimental/vscode-agent-pane packages/vscode-agent-pane`** — 履歴を保ったまま移動する。生成物 (`node_modules/`, `out/`, `.vscode-test/`) は移動前に削除する。
3. **path 参照の更新** — 以下の path 依存を新 path へ更新する:
   - `packages/vscode-agent-pane/scripts/open-dev-host.sh` の `repo_root` 解決 (`../..` 前提は深さが同じため変更不要だが、実行確認する)
   - `.vscode/launch.json` (PoC dir 内) の `${workspaceFolder}/../..` 参照
   - `src/test/e2e/runTest.ts` の `repoRoot` 解決
   - README / contract docs 内の `experimental/vscode-agent-pane/` 表記
4. **catalog 更新** — `.mozyo-bridge/docs/catalog.yaml` の `fc-vscode-agent-pane-poc` patterns を `packages/vscode-agent-pane/**` へ変更し (convention id / name も PoC 表記から見直す)、`coverage_roots` に `packages` を追加、`experimental` root の要否を判断する。`mozyo-bridge docs generate-file-conventions` で再生成し `--check` を通す。
5. **experimental 残骸整理** — `experimental/` 配下に残るものが無いことを確認する。残す物は contract docs (`vibes/docs/specs/**`) のみで、これは元々 `experimental/` 外にある。
6. **検証 (#11533)** — clean clone から `npm run dev:host` 手動 smoke と `npm run test:e2e` を実行し、`packages/` path で起動・成功条件 (README の Success Criteria) を満たすことを記録する。
7. **Python package 非混入確認** — `mozyo-bridge release check` 相当の artifact 検査で extension が sdist / wheel に含まれないことを確認する。

## Submodule / 独立 Repo 昇格条件

以下のいずれかが成立した時点で、monorepo subproject から独立 repo (+必要なら親 repo への submodule 登録) へ昇格する。判断は Redmine issue として起票し、owner が決める。

- extension が単独の release 対象になる (Marketplace publish または vsix 配布を開始する)。
- Marketplace publish pipeline / secrets が mozyo-bridge 本体の CI と分離が必要になる。
- 外部利用者向けの issue 管理 / version 管理が必要になる。
- extension の CI 時間や依存衝突 (Node / VS Code download 等) が本体開発を阻害する。

### 昇格時に決めること (昇格 issue の必須項目)

- 独立 repo 名 (候補: `mozyo-vscode-agent-pane`) と ownership。
- submodule にするか、独立 repo + contract docs 参照のみにするか。submodule 採用時は親 repo 側の pin 更新運用 (更新 trigger、CI での submodule checkout、version 整合) を整理する。
- mozyo_bridge repo に残す物: `vibes/docs/specs/vscode-agent-pane-contract.md` と本 doc を含む integration docs のみ。実装・E2E・拡張固有 docs は移す。
- publish 方針 (publisher id、license、telemetry なし等)。

## Non-Goals

- 本 doc は移行の実行記録ではない。実行は #11532 / #11533 の journal を正本とする。
- Marketplace 公開判断、repo 名の最終確定、submodule 運用詳細は昇格条件成立時の issue で決める。本 doc は判断材料と必須項目のみ規定する。
