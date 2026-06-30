# Redmine Version 操作面の安全境界 (Redmine #12651)

親 US #12643 (Redmine Version 名から package release 番号を切り離す) の子 #12651。
Redmine Version の rename / close / lock / delete と Version 内 open leaf issue 列挙を
**安全に実行する手段**を確定し、不足分を operator UI / REST wrapper / MCP 拡張へ分解する
設計正本。package release 番号決定とは分離する (release は [logic-release-flow] 側)。

## 1. 決定: 安全操作面は「判断 (pure preflight) と実行 (out-of-band) を分離する」

現行 provider surface は破壊的 Version 操作を実行できない (§4)。そこで本 US は
**実行ではなく判断面**を pure domain として確定し、実行は operator UI / 将来の
double-opt-in live adapter に委ねる。OOP-first 方針 ([logic-object-oriented-architecture-policy])
に従い、Redmine adapter Feature (`f_120_redmine_adapter`, [spec-bounded-context-map]) 配下に
domain + port を置く。

### 1.1 operation preflight (`domain/redmine_version_operation.py`)

`decide_version_operation(request) -> VersionOperationDecision` は fail-closed:

- **operation 語彙は閉じている** (`VERSION_OPERATIONS = rename|close|lock|delete`)。
  未知 operation は `unknown_operation:<op>` で blocked、実行 step を出さない。
- **全 operation が confirmation token を要求する** (rename を含む)。token は
  `"<op>:<version_id>"` で operation × target 固有。欠落/不一致は `confirmation_required`。
- **rename**: `new_name` 必須・現名と異なること・`classify_version_name` が
  `package_numbered` (`^v?\d+\.\d+...`) を返す名前は `new_name_package_numbered` で blocked。
  これが #12643 の「Redmine Version 名 = planning bucket、package 版番号にしない」を機械強制する。
- **close / lock**: counts 未確認 (`counts_known=False`) は `counts_required` で blocked
  (欠落/default の `open_issues_count=0` を「open issue 無し」と誤読しない)。counts 既知で
  `open_issues_count > 0` は既定 blocked (`open_issues_present`); 明示 `allow_open_issues`
  で許可するが warning を残す。既 closed/locked への再操作は blocked。未知 status は fail-closed。
- **delete (唯一の不可逆操作)**: counts 未確認は `counts_required` で blocked
  (欠落/default count を「空」と解釈しない fail-closed)。counts 既知かつ
  `issues_count == 0` **かつ** `open_issues_count == 0` **かつ** `closed_issues_count == 0`
  の真に空な Version のみ許可 (三者確認で矛盾 snapshot も blocked)。非空は `version_not_empty`、
  `historical_protected` 指定は `historical_protected` で blocked (歴史保持は close/lock 領域)。
  `VersionState.counts_known` は既定 False (fail-closed); `from_mapping` は 3 count field
  全て存在 **かつ非負整数として parse 可能** な時のみ True (parse 不能・負値は trusted zero
  にしない; `_coerce_count` が None を返し counts_known=False)。CLI inline path も
  `from_mapping` を経由して同一規則を適用 (正本一箇所)、`--versions-json` も同様。
- 全 guard 通過時のみ `rest_step` (`PUT/DELETE /versions/<id>.json`) と
  `operator_ui_step` を出す。pure・network 無し・mutation 無し。

### 1.2 open leaf 列挙 (`domain/redmine_version_enumeration.py`)

`enumerate_from_source(source, version_id)` は flat
`GET /issues.json?fixed_version_id=<id>` snapshot を読み、open leaf を返す read model。
leaf 規則: open issue は、同 set 内の別 open issue から `parent_id` 参照されない限り leaf。
parent でも子が全て closed なら leaf。tracker 別 count と open non-leaf を併記する。
これは MCP US-only surface が出せない読み筋 (§3)。`RedmineVersionIssueSource` port 経由で
live HTTP adapter を後から同 seam に差せる。

### 1.3 advisory CLI (`redmine-version` family)

- `redmine-version list-open-leaf --version-id ID --issues-json PATH`
- `redmine-version preflight --version-id ID --op OP [--new-name|--confirm|--allow-open-issues|--historical-protected] [--versions-json PATH | inline counts]`

両 subcommand は operator export の JSON snapshot を読み、判断を表示するのみ。
Redmine write も network call も行わない。blocked preflight は非 0 終了。

## 2. 残置 write port (live executor は未配線)

`RedmineVersionWrites` Protocol (rename/close/lock/delete) は seam として宣言するが、
本 slice では **live 実装を出さない**。理由は §4。live adapter は
`redmine_note_transport` の double-opt-in (CLI opt-in + env gate) 先例に従い、
`allowed=False` の decision を拒否すること。

## 3. open leaf 列挙: MCP US-only surface が不足である根拠

`list_user_stories(version_id=)` は UserStory tracker のみ返す。
`get_project_structure(version_id=, max_depth=4)` は Epic→Feature→US を top-down に歩き、
Version に直接付いた Task/Test/Bug leaf (親 US が同 Version に無いもの) を落とす。
本 US で Version #247 (open 2) に対し `get_project_structure` を実行した結果、
`total_tasks=0 / total_bugs=0 / total_tests=0` で leaf を 1 件も列挙できないことを確認した
(#226/#247/#248/#254 は open issue count を持つ)。よって flat issue-by-version 読み筋が必須。

## 4. capability gap と次 issue 分解

実行面が blocked である根拠 (本セッション shell):

- MCP `redmine_epic_grid` は `list_versions`/`create_version`/`assign_to_version` のみ。
  Version rename/close/lock/delete も flat `issues-by-version` 列挙 tool も無い。
- repo の Redmine adapter は read-only-by-design (GET issues / PUT issue-note のみ)。
  Version endpoint も DELETE も無い。
- `MOZYO_REDMINE_*` REST credential が本 shell に無く、匿名 REST 不可。

分解 (提案 next issue split):

1. **flat issue-by-version 読み adapter**: `RedmineVersionIssueSource` の live 実装
   (`GET /issues.json?fixed_version_id=<id>&status_id=*`、credential gate)。read-only。
2. **Version write live executor**: `RedmineVersionWrites` の live 実装
   (`PUT /versions/<id>.json` rename/close/lock、`DELETE /versions/<id>.json`)。
   double-opt-in fail-closed、§1.1 decision の `allowed` のみ実行。
3. **即時 cleanup は operator UI**: 上記未配線の間、rename/close/lock/delete は
   Redmine UI で実行し、before/after count を journal に残す。`preflight` の
   `operator_ui_step` をそのまま手順に使う。
4. **任意**: MCP 拡張 (`list_issues_by_version` / `update_version`) は長期的に
   より良い surface だが本 US の前提ではない。

## 5. 非目標

- package release 版番号決定 / tag / publish / version bump ([logic-release-flow])。
- 破壊的 REST の実配線 (本 slice では port 宣言のみ)。
- coordinator sublane の lane 運用変更 ([logic-coordinator-sublane-development-flow] は不変)。

参照: [logic-release-flow] / [logic-object-oriented-architecture-policy] /
[logic-source-layout-bounded-context-migration] / [spec-bounded-context-map] / [spec-project-map].
