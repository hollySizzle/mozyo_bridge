# Redmine Rails Agent Workflow

## Layered Source

この preset は Rails 開発用の Redmine preset である。まず汎用 Redmine workflow を読む:

- `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md`

この file が存在しない場合は、読んだふりをせず `mozyo-bridge rules install` を依頼して停止する。以下は Rails project にだけ追加する guardrail であり、汎用 Redmine preset を置き換えない。

加えて、scaffold 生成された `AGENTS.md` / `CLAUDE.md` は **thin router** であり、target Rails repo の project-local guardrail を置き換えるものではない。詳細は次節 `Project-Local Layer` を読む。

## Project-Local Layer (do not erase on scaffold apply)

成熟した Rails repo に対する `mozyo-bridge scaffold apply redmine-rails` は **新規 install ではなく re-sync** として扱う。scaffold preset は以下のカテゴリの project-local fact を **target repo 側に既に存在することを前提に** layering する。これらを scaffold output だけで覆い被せない。

- **(a) App stack identity** — Ruby version、Rails version、frontend stack (Hotwire / React / その他)、DB、deployment target、workspace path。
- **(b) Rails extension conventions** — project が採用している Rails 拡張 (Presenter layer / decorator / form object / service object など) と、その保管 directory 規約。
- **(c) Read-only documentation areas** — 仕様 directory を read-only 扱いするルール、編集禁止 path。
- **(d) Project-specific safety commands** — project 固有の DB 再生成 script、test runner の必須環境変数 (例えば test 用 DB 環境変数を明示しないと development DB が壊れる、など)、parallel test runner、JS test runner、log capture、lint / 静的検査の起動口。安全要件 (誤った command で project local DB が壊れる等) を含むので scaffold base に持ち上げない。
- **(e) Project docs governance** — project 固有の docs catalog、active-doc resolver script、nagger 生成物の場所と更新手順・編集禁止ルール。`mozyo-bridge` は generic な reading order を扱い、project 内 catalog の中身は target 側で版管理する。
- **(f) Local role-boundary overrides** — Redmine gate / オーナー指示で明示された local 例外 (特定種類のファイルだけは Codex 直接編集可、など)。汎用 preset は Claude 実装 / Codex 監査 の標準 split を扱うが、project local 例外はここに残らない。
- **(g) Project tooling and private convention** — local skill 同期 script、private internal tooling、project 固有 path / file convention。

(a)–(g) の本文は scaffold preset に持たない (host を跨いで再利用できない / project 内に正本がある)。preset release のたびに上書きしない。`scaffold diff redmine-rails` の `-` 行に (a)–(g) のいずれかが出ているなら、それは "scaffold output が project-local layer を erase しようとしている" シグナルである。apply 前に保存と merge を計画する。

## Project-Local Layer Apply Discipline

mature Rails repo に対する `scaffold apply redmine-rails` の手順:

1. `mozyo-bridge scaffold diff redmine-rails --target <repo>` で差分を観察する。`-` 行が `Project-Local Layer` (a)–(g) を含むなら、まだ apply しない。
2. project-local layer の本文を repo の AGENTS.md / CLAUDE.md / project-local docs に保存してから apply する。または、scaffold base 側の更新が中央 preset 参照行・version label・generator 行だけに限定されている (`scaffold.json` の `preset_version` / `generated_by` 差分が主) と判断できる場合に限り `--backup` で apply し、`.bak.<timestamp>` から project-local layer を手作業 merge する。
3. `--force` は project-local layer をバックアップ無しで消す可能性があるので、mature repo では使わない。fresh `./tmp/mb-smoke-*` などの isolated target にだけ使う。
4. project-local layer を `AGENTS.md` / `CLAUDE.md` の "scaffold base + project-local additions" の追記構造に整理しておくと、次回 re-sync の merge コストが下がる。
5. `scaffold status` は preset_hash と router file hash で drift を検出するため、project-local layer を追記した状態では `drifted` を出す。これは「project-local 追記がある」表示であり、scaffold base が壊れているわけではない。drift 内容と scaffold base が同期しているかを `scaffold diff` で別途確認する。

## Rails Scope Posture

- scope は controller / model / view だけではない。route、authorization、validation、transaction、migration、seed、background job、mail、cache、asset、Hotwire / Turbo / Stimulus、system spec、manual verification、existing URL / data compatibility を含む。
- Rails 固有の判断を owner 判断に丸投げしない。DB integrity、authorization、route compatibility、migration safety、Hotwire flow、test strategy は design consultation 候補である。
- Rails project 固有 path、private catalog、custom resolver、file convention は shared `redmine` preset へ戻さない。この Rails preset でも必須依存にせず、project が提供している場合にだけ使う。

## Rails Start Gate Additions

Start Gate では汎用 Redmine fields に加えて、以下を確認する。

- 対象 Rails app root、Rails version、Ruby version、DB、test framework、frontend stack (Hotwire / React / none など)。
- 変更対象の route、controller action、model、table、view / component、job / mailer、policy / permission、既存 URL / API / data compatibility。
- project が提供する Rails 規約 docs、active-doc resolver、generated file conventions があれば、それで対象 path に紐づく docs を解決し、本文を読む。

## Rails Design Consultation Triggers

以下は原則として実装前に Design Consultation Gate の候補にする。

- migration shape、data backfill、rollback strategy、既存データ補正、nullable / default / index / foreign key の変更。
- authorization / permission / role boundary、tenant boundary、admin / user / public の表示差。
- route / URL / parameter / API response の互換性変更。
- model callback、transaction、locking、dependent destroy、counter cache、N+1、query shape の変更。
- Hotwire / Turbo frame / Turbo stream / Stimulus controller の責務境界と画面遷移。
- service / form / presenter / decorator などの責務追加。
- seed / fixture / factory / test data の意味変更。

## Rails Implementation Done Additions

Implementation Done Gate では、汎用 fields に加えて以下を記録する。

- changed Rails layers: routes、controllers、models、views/components、helpers、services、jobs、mailers、policies、migrations、seeds、tests。
- migration がある場合: rollback 可否、既存データ影響、lock / downtime risk、backfill 方針、schema dump 更新有無。
- UI / Hotwire 変更がある場合: 操作手順、確認した画面 state、screenshot / capture の有無。
- authorization 変更がある場合: actor、許可 / 禁止 case、test coverage。
- existing URL / data compatibility を維持したか、変更する場合は owner / design approval の anchor。

## Rails Review Focus

Review Gate は次の順に見る。

1. **Data / migration safety** — data loss、irreversible migration、long lock、既存 record 不整合、schema dump 漏れ。
2. **Authorization / tenant boundary** — 権限漏れ、他 tenant data 参照、admin/user/public 差分。
3. **Route / compatibility** — 既存 URL、API、params、redirect、bookmark、external link、stored data との互換性。
4. **Rails layer responsibility** — controller 肥大化、model callback 乱用、service / form / presenter の責務不明瞭、view helper への business logic 混入。
5. **Hotwire / UI behavior** — Turbo frame / stream の target、progressive enhancement、validation error 表示、戻る / reload / duplicate submit。
6. **Testing / verification** — model / request / system spec、policy spec、migration check、manual verification、screenshot / capture。

style は上記の後に扱う。好みだけの Rails 流儀指摘を重大 finding にしない。

## Rails Verification Discipline

project の authoritative commands を優先する。存在しない場合の標準候補は次の通りだが、project ルールがあればそちらに従う。

- Ruby / Rails static checks: `bundle exec rubocop`、`bundle exec brakeman`。
- Tests: `bundle exec rails test`、`bundle exec rspec`、または project が定める subset。
- DB / migration: `bundle exec rails db:migrate`、`bundle exec rails db:rollback STEP=1`、`bundle exec rails db:migrate:status`。本番相当 data volume で危険な migration は local success だけで安全扱いしない。
- UI / Hotwire: system spec、browser / screenshot / generated capture、manual operation flow。

command が存在しない、重い、または実行不能な場合は、未実行理由と代替確認を Redmine journal に残す。`looks fine` は verification record ではない。

## Rails QA / Production Verification

QA Verification Gate では、仕様から操作手順を作り、implementation detail から期待値を作らない。

- failure report は reproduction、expected、actual、environment、browser、user role、record state、screenshot / log、bug / spec misunderstanding / unnecessary work の仮判定を分ける。
- 本番確認が必要な変更では Production Verification Gate に deploy version、migration status、確認 user / role、確認 record、rollback / follow-up 要否を残す。
- seed / data correction / migration 後の確認は、画面表示だけでなく DB state または observable behavior を確認する。

## Rails Prohibitions

- 汎用 `redmine` preset に Rails 固有 app path、private docs catalog、controller / model / migration 規約を混ぜ戻さない。
- migration、authorization、data correction を review なしで close しない。
- destructive migration、bulk update、external notification、production operation を owner approval / project operation rule なしに実行しない。
- Redmine journal、commit message、README、logs に credential、token、個人情報、production data excerpt を記録しない。
