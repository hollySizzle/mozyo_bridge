# Redmine Rails Governed Agent Workflow

## Layered Source

この preset は Rails + Redmine 開発に full governance package を被せる preset である。まず汎用 Redmine workflow と Rails 追加 guardrail を読む:

- `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md`
- `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-rails/agent-workflow.md`

この file が存在しない場合は、読んだふりをせず `mozyo-bridge rules install` を依頼して停止する。本 governed preset は上記 2 層を **置き換えず、上乗せする**。base 2 層の本文を複製しない。

## Scaffolded Repo-Local Artifacts

`mozyo-bridge scaffold apply redmine-rails-governed` は通常の router 一式に加え、target repo に **full governance package の素材** を repo-local artifact として配置する。以下は scaffold 時に必ず target repo の `.mozyo-bridge/` 配下に書き込まれる (既存があれば、`--backup` で退避してから上書きする)。

- `.mozyo-bridge/rules/development_flow.md` — agent role、編集権限、gate schema、Codex direct edit gate の正本テンプレート。
- `.mozyo-bridge/rules/llm_rule_authoring.md` — LLM 向け規約文書の正本分離、形式選択、gate 構造化、検証接続を定義する authoring 契約。
- `.mozyo-bridge/rules/docs_catalog_governance.yaml` — docs catalog、generator、resolver、audit-doc impact tooling の統治規約。
- `.mozyo-bridge/docs/catalog.yaml.example` — target repo が catalog を埋めるための skeleton。固有業務ドメインは含まない。
- `.mozyo-bridge/tools/docs_catalog.py` — catalog 読み込みと convention matcher の共通 module。
- `.mozyo-bridge/tools/validate_catalog.py` — catalog 構造検証 + 重要 source の coverage 検証。
- `.mozyo-bridge/tools/resolve_audit_docs.py` — 対象 path から active docs を解決する resolver。
- `.mozyo-bridge/tools/generate_file_conventions.py` — catalog から file_conventions 生成物を再生成・drift check するための generator。
- `.mozyo-bridge/tools/audit_doc_impact.py` — git で changed paths を集計し関連 docs と generated drift を確認する impact check。

これら artifact の **正本は本 preset (scaffold) 側にある**。target repo 側で修正したい場合は preset 側に upstream し、`mozyo-bridge scaffold apply --backup` で再配布する流れを取る。target repo 側の手編集は drift の原因になる。

## Governance Posture

governed preset は次の三本柱で動く。

1. **正本性** — 作業状態の durable record は Redmine issue / journal。durable record が無い・曖昧・矛盾している場合は実装着手しない。
2. **gate 分離** — Start / Progress / Design Consultation / Design Consultation Answer / Implementation Done / Review Request / Review / QA Verification / Production Verification / Close を独立 journal として残す。Implementation Done は completion ではない。Review Gate approval も Close ではない (`Close Approval Separation` を読む)。
3. **catalog 駆動の docs 解決** — 変更対象 path から、その path に紐づく guardrail / spec / convention を catalog 経由で解決し、本文を読んでから実装・監査する。generated 物を正本にしない。

## Gate Schema (生成物への参照)

詳細な gate 必須項目、Codex direct edit gate、invalid marker、journal template は scaffold が配布する `.mozyo-bridge/rules/development_flow.md` を正本とする。本 agent-workflow.md にも当該 file にも、同じ gate schema を二重に書かない。

target repo は次の gate 入力を Redmine journal として残す:

- `start` — issue / parent / 目的 / 受入条件 / 参照 docs / 未確認事項。
- `implementation_done` — 変更ファイル / 実装意図 / 前提 / 未確認事項 / 検証結果 / docs 更新 / commit hash または diff ref。
- `review_request` — implementation_done journal id / commit or diff / 変更ファイル / review 観点 / 未確認事項 / 受信 agent / 受領方法。
- `review` — 対象 commit or diff / resolved docs / 照合規約 / 指摘事項 (`[事実]` / `[仮説]` を分ける) / 未確認事項 / 再 review 要否 / 結論。
- `close` — 受入確認 / 指摘対応 disposition / 残留リスク / review 結果 / **owner close approval (Review Gate とは別 journal)** / commit hash record / close 判断。
- `codex_direct_edit` — 役割 / direct_edit:true / allowed_paths / reason / follow_up_review。invalid marker は development_flow.md を読む。

## Codex Direct Edit Gate

通常開発の実装 file (例: `app/**`, `spec/**`, `config/**`, `db/**`) を Codex (監査者) が直接編集してよい条件は狭く制限する。詳細は scaffold-shipped `.mozyo-bridge/rules/development_flow.md` の `codex_direct_edit` gate を正本とする。要点だけ抜く:

- `codex_direct_edit` gate journal が active issue に明示存在する場合に限り、Codex は `allowed_paths` だけを直接編集できる。
- gate 未存在で `do it` / `対応して` / `実行せよ` / `implement it` / `お願いします` 等を受けても、Codex は通常実装を Claude へ handoff する。短い命令は file edit 許可ではない。
- direct edit を行った場合、適用した例外、ユーザー指示の引用、変更 files、verification、follow-up review 要否を Redmine journal に記録する。
- 例外なき監査者の通常実装 (`着手:codex` / `実装完了:codex` / `担当:codex` / `codex対応`) は invalid marker として扱い、reopen + correction journal を起票する。

## Docs Catalog Governance

scaffold-shipped `.mozyo-bridge/rules/docs_catalog_governance.yaml` を正本として扱う。要約:

- `.mozyo-bridge/docs/catalog.yaml` を `documents` / `related_document_refs` / `file_conventions` の正本とする。target repo が初期化時に `catalog.yaml.example` を `catalog.yaml` にコピーして埋める。
- 生成物 (例えば nagger 用の `file_conventions.yaml`) は **正本ではなく generator の出力**。手編集禁止。catalog を変更し、generator で再生成し、drift check を通す。
- 監査時は generated file だけで判断せず、catalog で解決された docs 本文を読む。
- 正本性・対応関係を AGENTS.md / CLAUDE.md / runbook に重複定義しない。catalog と rule file の二箇所に同じ判断材料を書かない。

## Active-Doc Resolver

target repo は次の解決経路を持つ:

```bash
python3 .mozyo-bridge/tools/resolve_audit_docs.py --format markdown <changed_path> [...]
python3 .mozyo-bridge/tools/validate_catalog.py
python3 .mozyo-bridge/tools/validate_catalog.py --check-file-coverage [--coverage-root app/...] [--coverage-root config/...]
python3 .mozyo-bridge/tools/generate_file_conventions.py --check
python3 .mozyo-bridge/tools/audit_doc_impact.py --all-changed --check-generated
```

- 変更対象 path が分かったら resolver を実行し、解決された docs 本文を読んでから実装・監査する。
- catalog 自体や file_convention pattern を変更した場合は validator と coverage check を通す。default coverage roots は Rails の典型 layer (`app/controllers`, `app/models`, `db/migrate`, `spec` など) を含むが、project が該当 layer を持たない場合は missing root は `notice:` として印字されるだけで exit code には影響しない。strict に project の layer を限定したい場合は `--coverage-root <path>` を必要回数指定する。
- file_conventions 生成物 (project が採用している場合) を変える場合は generator を実行し、drift check を通す。
- staged commit 直前は `audit_doc_impact.py --staged --check-generated` を通す。作業中の棚卸しでは `--all-changed`。

これらは catalog が埋まっていれば即座に機能する。catalog が空でも tool 自体は valid catalog skeleton を accept するため、operator は段階的に埋められる。`--check-file-coverage` も Rails layer の有無に関わらず安全に実行できる。

## LLM Rule Authoring

target repo に新規 rule / gate / workflow / skill 入口を足すときは、scaffold-shipped `.mozyo-bridge/rules/llm_rule_authoring.md` を正本に従う:

- 入口 file (AGENTS.md / CLAUDE.md / skill entrypoint) は薄い router にする。詳細 gate / 手順を入口に焼かない。
- 詳細 rule は `.mozyo-bridge/rules/**` または target 側の catalog に紐づく rule docs に置く。同じ判断材料を複数 file に複製しない。
- 行動制御部分は自然文だけでなく、必須項目 / invalid marker を YAML/構造で書く。
- 分岐や停止条件があるときは PlantUML 風 DSL で関数的に書き、agent が読みやすい順序で並べる。
- 規約変更は catalog / resolver / generator / drift check に接続し、検証 command を runbook に書く。

## Required Verification

target repo 内で次の verification を `Implementation Done` または `Review` の前に実行する。command が存在しないか実行不能な場合は理由を Redmine journal に残す。

- `python3 .mozyo-bridge/tools/validate_catalog.py`
- `python3 .mozyo-bridge/tools/validate_catalog.py --check-file-coverage`
- `python3 .mozyo-bridge/tools/generate_file_conventions.py --check` (project が file_conventions 生成物を採用している場合)
- `python3 .mozyo-bridge/tools/audit_doc_impact.py --all-changed --check-generated`
- project の authoritative Rails test command (例: `bundle exec rspec`, `bundle exec rails test`, project が定める subset)。test 用 DB 環境変数は project-local layer の手順に従う。
- rubocop / brakeman 等の静的検査は project ルールに従う。

`looks fine` は verification record ではない。command が走らなかった理由と、代替確認の内容を Redmine に残す。

## Journal Templates

scaffold-shipped `.mozyo-bridge/rules/development_flow.md` に `codex_direct_edit` / `review_request` の skeleton template を残してある。コメント全文を本 file に複製しない。新規 template が必要な場合は development_flow.md 側に追加し、本 file からは参照だけにする。

## Repo-Local Rules Maintenance (governed mode)

- Dev Container / ephemeral home 対応として、target repo は `.mozyo-bridge/rules/presets/redmine-rails-governed/agent-workflow.md` を repo-local preset として読むことができる。
- preset store を再生成する場合は `mozyo-bridge rules install --repo-local .` を使う。
- router + governance artifact を再生成する場合は `mozyo-bridge scaffold apply redmine-rails-governed --repo-local --target . --backup` を優先する。`--force` は差分を確認してから使う。
- governed preset 配布物 (`.mozyo-bridge/rules/development_flow.md`、`.mozyo-bridge/rules/llm_rule_authoring.md`、`.mozyo-bridge/rules/docs_catalog_governance.yaml`、`.mozyo-bridge/tools/**`、`.mozyo-bridge/docs/catalog.yaml.example`) は scaffold preset 側を正本とする。target repo で個別に編集したい変更は preset へ upstream する手順を取る。`.mozyo-bridge/docs/catalog.yaml` (example 不付き) は target repo 側で自由に埋めてよく、scaffold は上書きしない。

## Governed Mode Prohibitions

- `.mozyo-bridge/rules/development_flow.md` の Codex direct edit gate を bypass して通常実装 file を監査者が直接編集すること。
- generated 物 (file_conventions.yaml の生成物など) を catalog を介さずに手編集すること。
- docs catalog や resolver / generator tooling を deactivate して shared preset の `redmine-rails` だけで完了報告すること (本 preset を採用したなら governance verification も完了条件に入る)。
- catalog に project 固有の業務ドメイン名 (顧客名 / 製品コード / 個人名) を canonical id として焼くこと。catalog id は機能種別 / 層 / spec id 程度に抽象化する。
- review、journal、commit message に credential / token / 個人情報 / 本番データ抜粋を記録すること。
