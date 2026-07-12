# herdr live pane 再配置 runbook (operator 向け)

live な herdr pane pair (coordinator + auditor / gateway + worker 等) の **位置交換 (swap)** と **split 方向変換 (左右 ⇔ 上下)** を、実機で検証済みの手順として replay 可能な形で固定する。2026-07-12 の live 実測 (herdr 0.7.1) で確立した recipe と、その安全境界・herdr 側 gap を記録する (Redmine #13648 / #13664)。

対象は **手動 CLI での live 再配置** のみ。設定駆動の恒久配置 (`pane_placement`) は本書の非 scope で、境界は下記「設定駆動配置との境界」を読む。設計正本は [[spec-herdr-native-identity]] (target authority = herdr assigned name)、lane 運用手順の正本は [[task-herdr-lane-operations]]、pane identity / marker の意味構造は [[logic-pane-centric-cockpit-semantics]]。本書は手順のみを扱い規約本文を複製しない。

## 適用範囲と非 scope

- **scope**: 既に launch 済みの live pane pair を、operator が herdr の CLI で **その場で** swap / split 方向変換する手順と安全性の根拠。
- **非 scope**:
  - source / runtime 変更 (mozyo-bridge への wrapper command 追加は #13646 系の別 US)。
  - herdr 本体の改修 (same-tab re-split / rotate action の追加)。
  - 設定駆動の恒久配置 (`.mozyo-bridge/config.yaml` の `pane_placement`。#13646 / #13647)。
  - live pane actuation の自動化、外部送信、release、origin/main への push。
- herdr の pane 操作は外部 binary (`herdr`) の CLI であり mozyo-bridge の command ではない。argv の細部は実行時に `herdr pane --help` / 各 subcommand の `--help` を正本にする (本書は 2026-07-12 実測の verified 形のみ literal に固定し、未記録の signature は推測で埋めない)。

## 前提 / 用語

- **target identity は assigned name 権威**: pane の route authority は herdr assigned name (durable identity) + live inventory であり、pane 位置・tab 配置・pane id は権威ではない ([[spec-herdr-native-identity]])。再配置は表示位置を変えるだけで、assigned name / route / projection を変えない。操作前に必ず対象 pane の assigned name と live 状態を確認し、pane id を durable な target として扱わない。
- **tab join の権威は `tab_id`**: どの pane が同一 tab に属するかは live inventory の `tab_id` のみが authority で、tab label は cosmetic (#13411)。bounce で「元の tab へ戻す」際は label ではなく元 tab の `tab_id` を指定する。
- **live pair の即時再配置経路はこの recipe のみ**: herdr は same-tab re-split を拒否するため (下記)、`pane_placement` 設定を将来足しても既存 live pair の配置は変わらない。live で今すぐ入れ替える唯一の経路が本 recipe である (#13648)。

## herdr 0.7.1 の制約 (2026-07-12 実測)

- **same-tab re-split は 1 発 API が無い**: 同一 tab 内で split 方向を変換する直接 command は存在しない。`herdr pane move <id> --tab <同一 tab_id>` は `changed:false` / `reason:same_tab` を返す **no-op** で、方向は変わらない。
- **`herdr pane swap` は位置交換のみ**: pair の左右 (または上下) の位置を入れ替えるだけで、split の **方向** は変換しない。
- **方向変換は 2 段 bounce が必要**: 一旦別 tab へ退避してから、元 tab へ望む split 方向で戻す (下記 recipe)。

## 検証済み recipe

いずれも操作前に対象 pane の assigned name と live 状態を確認し (target identity 権威)、期待した pane を掴んでいることを実測してから実行する。失敗・想定外の応答 (`changed:false` 等) が出たら停止し、blind に再実行しない (fail-closed)。

### A. 位置交換 (swap)

同一 tab 内で 2 pane の位置だけを入れ替える (split 方向は保つ)。herdr 0.7.1 の `herdr pane swap` は 2 形式を持つ。

```sh
# 明示 2-pane 指定: source と target の位置を交換する
herdr pane swap --source-pane <pane-a> --target-pane <pane-b>

# 方向指定: 対象 pane を指定方向の隣接 pane と交換する (TUI の swap_pane_{left,right,up,down} に対応)
herdr pane swap --direction left|right|up|down [--pane <pane-id>|--current]
```

- 効果: 対象 pane の表示位置を交換する。split の方向は変えない。assigned name / route は不変。
- 上記は herdr 0.7.1 の `herdr pane --help` で確認した verified signature。positional の `herdr pane swap <pane-a> <pane-b>` 形式は **存在しない** (指定は `--source-pane` / `--target-pane` か `--direction`)。

### B. 左右 → 上下 (down 化) の 2 段 bounce

左右 split の pair を上下 split に変換する。片方の pane を一時 tab へ退避し、残す pane の **下** へ戻す。

```sh
# 0. 現状確認: pane id と元 tab の tab_id を list で確認、現在の split 方向は layout API で実測
herdr pane list [--workspace <workspace_id>]
herdr pane layout [--pane <pane-id>|--current]   # ← 現在の split 方向 (direction) を読む layout API

# 1. 退避: 動かす pane を新しい一時 tab へ移す
herdr pane move <moving-pane-id> --new-tab

# 2. 戻す: 元 tab へ、残す pane の下 (down) に戻す
herdr pane move <moving-pane-id> --tab <original-tab-id> --split down --target-pane <staying-pane-id>
```

- `--target-pane` は tab に残っている pane。`--split down` はその pane に対して戻る pane を **下** に配置する。
- 一時 tab は最後の pane が退去した時点で herdr が自動消滅させる (husk は残らない)。
- 検証: 戻した後に `herdr pane layout` で対象 pane の `direction` が `down` になっていることを実測確認する (#13646 close condition の「layout API 実測 `direction: down`」に対応)。

### C. 上下 → 左右 (right 戻し) の 2 段 bounce

recipe B と対称。`--split down` を `--split right` に置換して、上下 pair を左右へ戻す。

```sh
herdr pane move <moving-pane-id> --new-tab
herdr pane move <moving-pane-id> --tab <original-tab-id> --split right --target-pane <staying-pane-id>
```

- 検証: `herdr pane layout` で `direction` が `right` になっていることを実測確認する。

## 安全性の根拠 (実測)

2026-07-12 の live 実測で、上記 recipe が以下を保つことを確認した。

1. **assigned-name authority 不変**: 再配置は表示位置を変えるだけで、pane の assigned name / route / projection を変えない。handoff / dispatch の宛先解決は assigned name + live inventory 経由で自動追従する ([[spec-herdr-native-identity]])。
2. **agent process 無傷**: 退避・戻しの間に pane 内の Claude / Codex TUI process は終了・再起動しない。session / 会話状態は保持される (pane の移動であって kill / relaunch ではない)。
3. **一時 tab の自動消滅**: bounce で mint した一時 tab は最後の pane が退去した時点で herdr が自動 close する。手動 cleanup は不要で、husk は残らない。
4. **target identity 確認**: 操作前に対象 pane の assigned name と live 状態を実測し、pane id を durable target として扱わない。掴む pane を取り違えないことを再配置の前提にする。
5. **失敗時 fail-closed**: `changed:false` / `reason:same_tab` 等の想定外応答、または対象 pane の消滅を観測したら停止し、durable record に残してから判断する。blind な再実行・別 pane への当て推量操作をしない。

## herdr 側 API / TUI gap と upstream 追跡

- **CLI gap (herdr 0.7.1)**: same-tab re-split (同一 tab 内の split 方向変換) を 1 発で行う API は無い。`pane move --tab <同一 tab>` は `same_tab` で no-op、`pane swap` は位置交換のみ。方向変換は本書の 2 段 bounce が唯一の経路。
- **TUI gap (herdr 0.7.1)**: keybindable action として `swap_pane_left` / `swap_pane_right` / `swap_pane_up` / `swap_pane_down` は実在するが、split の **orientation 変換** (左右 ⇔ 上下) を行う action は binary の keybind 語彙 全走査でも不存在。
- **upstream 追跡状態**: herdr upstream への機能要望 (same-tab re-split / rotate action) は **2026-07-13 時点で未提出**。owner 方針により外部への投稿はしない (#13648: 「まあひとまずはじゃあいいよ、APIのみで」)。要望を提出する場合は owner 承認を得てから行い、提出後は本節にその追跡先 (upstream issue link) を追記する。それまでは本 recipe が回避策の正本。

## 設定駆動配置との境界

- 恒久的な pair 配置 (どの lane class を左右 / 上下、どちらの provider を先に置くか) を宣言駆動にする作業は別 US: `.mozyo-bridge/config.yaml` への閉集合 `pane_placement` block 追加が #13646、親子孫 3 層別 (lane-role 別) の keying が #13647。
- ただし `pane_placement` 設定は **新規 launch / heal 経路のみ** に効く。herdr が same-tab re-split を拒否するため、既存 live pair の即時再配置は設定変更では起きない。live で今すぐ入れ替える唯一の経路が本書の recipe である (#13646 Non-goals / #13648)。

## 記録の衛生

- journal / commit message に host-local 絶対 path や pane の内部 id を durable target として書かない。pane は assigned name / lane label / role で参照する (正本: [[rule-public-private-boundary]])。
- pane id は snapshot であり durable identity ではない。再配置の記録は「どの assigned name の pane を down/right に変換した」という identity 軸で残す。
