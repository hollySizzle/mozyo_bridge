# Herdr Unit board

Redmine #15114。Herdr 上で動いている mozyo 管理対象の agent pair を、担当
project、workflow role、責務、現在の lane、agent 状態で識別する表示機能の実装正本。

## 目的

複数 project の coordinator pair を同じ Herdr workspace / tab に並べると、pane の
位置や provider 名だけでは人間が担当を見分けにくい。Unit board は、既存の durable
identity と review 可能な role binding を live Herdr inventory に結合し、次を表示する。

- project 名
- 宣言済み workflow role
- 宣言済み project scope を用いた責務
- default lane または issue lane の読みやすい作業 label
- Claude / Codex の live runtime state

provider、pane の上下左右、tab、表示 title から role や責務を推測しない。role binding
がない場合は `unknown` / `missing`、矛盾する場合は `invalid` / `ambiguous` と表示する。

## CLI

```text
mozyo-bridge herdr unit-board show
mozyo-bridge herdr unit-board show --json
mozyo-bridge herdr unit-board sync
mozyo-bridge herdr unit-board watch
```

- `show` は一度だけ public-safe projection を表示する。
- `sync` は各 live pane の Herdr display metadata を更新する。
- `watch` は plugin-owned popup 内の terminal board を定期更新する。
- inventory を読めない場合や managed identity が壊れている場合は非0で終了する。

JSON payload は transient `pane_id`、absolute path、ticket本文、agent本文を含めない。
`pane_id` は同一process内の `sync` が action-time locator として使うだけで、projection
や durable state へ保存しない。
action-time locator は共通の `terminal_transport.valid_target` を満たす場合だけliveと
扱う。option形状、空白、metacharacterを含むlocatorは `reload_required` とし、metadata
commandを実行せず同期失敗として返す。

project / responsibility / work など外部由来の表示値は、JSON、text、Herdr display
metadataへ渡す前に同じpublic-safe projectionを通す。C0/C1とUnicodeの方向制御文字は
無害化し、POSIX / Windows / homeのabsolute path形状とcredential形状は固定値
`[redacted]`へ畳み、入力のbasename・key・valueを反映しない。
credential形状にはaccess-key assignment、authorization header、JWT形状を含める。
absolute path判定はrepo共通の
`terminal_runtime_provider/domain/absolute_path_rule.py`を再利用し、別の弱い判定を作らない。

`unit_id` とdisplay metadataの `mozyo_unit` は、raw identityを表示用の文字数で
切り詰めず、完全な `(workspace_id, lane_id)` から作る固定長のopaque digest keyとする。
project名やlane名の表示をidentityへ昇格させない。

text表示へ正の `--width` が与えられた場合、各出力行はそのterminal cell幅を超えない。
`watch` を含む全railはruntime解決失敗またはunavailable snapshotを非0で終了し、
例外本文・traceback・binary pathを表示せず、固定されたunavailable結果だけを返す。

## Herdr plugin

packaged manifest は `herdr-plugins/mozyo-unit-board/herdr-plugin.toml` に置く。

- startup と `pane.created` / `pane.agent_detected` event で display metadata を再同期する。
- cold restart 後も metadata を再構成し、過去の pane locator を再利用しない。
- popup action は `watch` だけを起動する。
- plugin command は `mozyo-bridge herdr unit-board` のみを呼ぶ。
- agent input、Redmine、workflow state、mozyo state DB、pane geometry は変更しない。

plugin は user-global に install / enable されるため、reviewed commit pin と manifest
identity を Herdr plugin policy で検査してから導入する。local development link は
production install の代替にしない。

## Authority boundary

| 表示値 | 読み取り元 | authority の扱い |
| --- | --- | --- |
| Unit identity | decoded managed Herdr assigned name | workspace / lane grouping key |
| project label | workspace registry | 表示のみ |
| workflow role / 責務 | repo-local workflow role binding | 宣言を表示。推測しない |
| work label | lane metadata | 表示のみ。ticket subjectとは断定しない |
| runtime state | action-time Herdr inventory | live observation |
| title / token | Herdr display metadata | 表示のみ。identityにしない |

この機能は workflow、routing、review、approval、completion、close の正本ではない。
UI が表示した pane の位置を delivery authority に使わない。

## Pane movement boundary

Redmine #15114 は識別表示のみを実装する。pane の配置変更は #14605 系の
preview-first safe action が所有する。

- 同一 tab 内の入替は Herdr `pane swap`、別 tab / workspace への移動は `pane move`
  という異なる操作である。
- apply 前に live pane と managed Unit identity を再照合する。
- native plugin API に drag-and-drop chrome はないため、最初の操作UIは keyboard で
  source / destination を選び、preview 後に実行する。
- `layout apply` は live process と scrollback を作り直しうるため、既存 pane の自由な
  移動手段として使わない。

## 検証

- pure grouping / ambiguity / control-character / terminal-width unit tests
- live inventory join と metadata command allowlist unit tests
- plugin manifest の UX-only static contract test
- independently reviewed commit pin の検査
- Z690 で3 coordinator Unitの識別、再起動後の再同期、既存agent input非変更を smoke

## 関連文書

- `vibes/docs/logics/herdr-plugin-presentation-consumer-boundary.md`
- `vibes/docs/logics/plugin-ready-adapter-boundary.md`
- `vibes/docs/logics/delegated-coordinator-cockpit-display.md`
- `vibes/docs/logics/unit-presentation-state-db.md`
- `vibes/docs/logics/pane-centric-cockpit-semantics.md`
- `vibes/docs/rules/public-private-boundary.md`
