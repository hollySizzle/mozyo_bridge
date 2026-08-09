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
mozyo-bridge herdr unit-board interact
```

- `show` は一度だけ public-safe projection を表示する。
- `sync` は managed agent inventory と完全な pane inventory を照合し、各 live pane の
  Herdr display metadataを更新する。agent終了後にshellへ戻ってagent inventoryから
  消えたpaneも、残存するnamespaced `mozyo_*` tokenから検出してclearする。
- `watch` は plugin-owned popup 内の terminal board を定期更新する。
- `interact` は同じpublic-safe Unit一覧をキーボードで選択する。`p`は専用2-pane
  pairの配置、`h` / `l`はshared tab内のUnit列の左 / 右移動、`-` / `+`はUnit列の
  相対幅の縮小 / 拡大をpreviewする。previewが実行可能な場合だけ`a`で明示applyする。
  previewは選択Unitの現在位置→目標位置と、実測幅share→目標幅shareを表示する。
  `j` / `k`は選択、`r`は再読取、`q`は変更せず閉じる。
- `interact` の選択keyは完全なidentityから作ったopaque `unit_id`とし、表示用に
  切り詰めた`workspace_id` / `lane_id`をaction入力へ戻さない。preview直前にlive
  inventoryから`unit_id`を完全なidentityへ一意に再解決できない場合はzero-writeで拒否する。
- apply呼出しが例外になった場合は、書込み前か書込み後かをUIから判定できないため
  `partial_failure / postcondition_failed`として表示し、refresh・状態確認まで再実行を
  禁止する。apply結果取得後のboard refresh失敗でも結果を失わず、同じくblind retryを促さない。
- inventory を読めない場合や managed identity が壊れている場合は非0で終了する。

JSON payload は transient `pane_id`、absolute path、ticket本文、agent本文を含めない。
`pane_id` は同一process内の `sync` が action-time locator として使うだけで、projection
や durable state へ保存しない。
action-time locator は共通の `terminal_transport.valid_target` を満たす場合だけliveと
扱う。option形状、空白、metacharacterを含むlocatorは `reload_required` とし、metadata
commandを実行せず同期失敗として返す。

Herdr 0.8 はplugin hookを別processで非同期実行するため、`sync` はoperator homeの
privateな空lock fileを `flock` し、inventory取得、metadata更新、inventory再照合を
1つのcross-process critical sectionとして直列化する。lockを取得・解放できない場合は
更新せず（解放失敗は既に行った更新件数を保持して）非0で返す。agent identityが更新中に
変わった場合はmutation直前の再取得で旧identityへのwriteを見送り、更新後にも再取得する。
固定回数だけ最新inventoryへ収束させ、安定しなければ
`reload_required` とする。

metadata reportにwall clock由来の `--seq` は使わない。lock下の到着順をそのまま採用する
ことで、時計の後退・同値や古いprocessの遅延完了をHerdrがexit 0のまま無視する経路を
作らない。過去に同じsourceへseq付きreportが残っていても、Herdr 0.8はseqなしreportを
受理する。

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

- startup と `pane.created` / `pane.agent_detected` / `pane.exited` event で display
  metadataを再同期する。agent releaseを通知する `pane.agent_detected` hook後は完全な
  pane inventoryのnamespaced tokenを照合し、agent inventoryから消えたshell paneの
  旧title/tokenをclearする。`pane.exited` はpane自体の終了通知であり、shellへ戻る
  agent終了とは同一視しない。
- cold restart 後も metadata を再構成し、過去の pane locator を再利用しない。
- popup pane は `interact` を起動する。選択変更・refresh・cancelは保留previewを破棄し、
  previewのないapply、refused / matched previewはpane commandを実行しない。
- plugin command は `mozyo-bridge herdr unit-board` のみを呼ぶ。
- agent input、Redmine、workflow state、mozyo state DBは変更しない。pane geometryの変更は
  pluginがraw pane APIを呼ばず、#14608のpreview-first serviceへUnit identityを渡した
  明示apply時だけ行う。serviceはapply直前にidentity・generation・geometryを再照合する。
- Unit列の左右移動と幅変更はlive geometryだけを1段階変更する。repo-local configと
  operator-local presentation stateへ暗黙保存しないため、再起動後の恒久配置は既存の
  `position` / `relative_width` 宣言が決める。

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

Redmine #15116 の最初の操作sliceは、#14608が扱う**1つの専用2-pane Unit内**の
split / provider順 / ratioを宣言済み設定へ収束する。Redmine #15122 はその次の
操作sliceとして、shared tab内の既存full-pair Unit列をkeyboardで選び、左右へ1列移動、
または相対幅を1段階変更するpreview-first actionを実装する。drag-and-drop、別tab / 別
workspaceへの移動、任意座標指定、配置の暗黙保存は実装済みとは扱わない。

- 同一 tab 内の入替は Herdr `pane swap`、別 tab / workspace への移動は `pane move`
  という異なる操作である。
- apply 前に live pane と managed Unit identity を再照合する。
- native plugin API にdrag-and-dropの操作面はないため、現在の操作UIはkeyboardで
  Unitと1段階の変更を選び、preview後に実行する。
- shared tabの全managed Unitが完全な2-pane列として同じHerdr workspaceに存在しない
  場合は変更しない。apply時にはUnit identity、generation、tab、geometryを再照合する。
- repo-local `position` / `relative_width` が欠落・不正でも、one-shot actionのpreviewは
  現在のlive順と実測幅だけから作る。preview形成中に順序・幅・authorityが変わった場合も
  zero-writeで拒否し、古い観測から別の隣接Unitを動かさない。
- `layout apply` は live process と scrollback を作り直しうるため、既存 pane の自由な
  移動手段として使わない。

## 検証

- pure grouping / ambiguity / control-character / terminal-width unit tests
- live inventory join と metadata command allowlist unit tests
- plugin manifest のpresentation-consumer static contract test
- independently reviewed commit pin の検査
- Z690 で3 coordinator Unitの識別、再起動後の再同期、既存agent input非変更を smoke

## 関連文書

- `vibes/docs/logics/herdr-plugin-presentation-consumer-boundary.md`
- `vibes/docs/logics/plugin-ready-adapter-boundary.md`
- `vibes/docs/logics/delegated-coordinator-cockpit-display.md`
- `vibes/docs/logics/unit-presentation-state-db.md`
- `vibes/docs/logics/pane-centric-cockpit-semantics.md`
- `vibes/docs/rules/public-private-boundary.md`
