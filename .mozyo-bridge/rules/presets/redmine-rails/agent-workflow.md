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

scaffold-generated `AGENTS.md` / `CLAUDE.md` には Project-Local Additions マーカー (`<!-- mozyo-bridge:project-local-additions:begin -->` ～ `<!-- mozyo-bridge:project-local-additions:end -->`) が含まれており、その間に書いた project-local layer 本文は `scaffold apply` / `scaffold diff` が機械的に保持する。

re-sync の手順:

1. 初回 `scaffold apply redmine-rails --target <repo>` 後、マーカー間にこの project の Rails / Redmine 固有事実 (Project-Local Layer (a)–(g) を埋める内容) を追記する。
2. 以降の re-sync では `scaffold diff redmine-rails --target <repo>` で **scaffold base 側の差分だけ** が表示される (マーカー間の project-local 追記は rendered template 側に substitute されるので diff から消える)。`-` 行に project-local 追記が出ている場合は、マーカー外に書かれているシグナル — マーカー内へ移動する。
3. `scaffold apply --backup` (推奨) は existing AGENTS.md / CLAUDE.md を `.bak.<timestamp>` に退避してから新しい router を書き、その新しい router にマーカー内の本文が substitute される。マーカー内に書いた project-local 追記は失われない。`.bak.*` は監査用 fallback。
4. `scaffold apply --force` は backup を作らずに上書きするが、マーカー preservation は同じく適用されるので、マーカー内の project-local 追記は保持される。マーカー *外* に書いた追記 (古い scaffold で marker pair の外側にあった内容) は上書きで消える。
5. legacy scaffold (router にマーカー pair が無い古い AGENTS.md / CLAUDE.md) は preservation 対象外。re-sync 前に project-local 追記をマーカー内へ移動するか、`--backup` で退避後に手作業 merge する。
6. `scaffold status` は preset_hash と router file hash で drift を検出する。マーカー間に追記しただけで scaffold base が同じ場合、status は `drifted` を表示する (router hash が manifest と一致しないため)。次の `scaffold apply --backup` を一度走らせれば manifest が現在のマーカー内本文の hash を記録し、status は clean に戻る (これが想定の運用)。

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

## Active-Doc Resolver Concept

project が "対象 path から、その path に紐づく guardrail / spec / convention doc 群を解決する" 仕組みを持つことがある (active-doc resolver、docs catalog、generated file-conventions ファイル、nagger 出力 など。具体的な script 名や file 名は project ごとに異なる)。preset 側は具体名を強制しない。下記の運用原則だけを共有規約とする。

- 変更対象 path が分かったら、project が active-doc resolver を提供している場合はそれで guardrail / spec / convention doc を解決し、本文を読む。catalog ファイルや generated guardrail ファイルがある場合は、その正本と編集禁止ルール (生成物を手編集しない、catalog を経由する、など) を必ず project-local layer に明記する。
- resolver / catalog / generator の **具体的な path や command** は project-local layer (router の Project-Local Additions マーカー間) に書く。preset 側に列挙しない。
- project がこれらを提供していない場合は、preset 側の `Rails Start Gate Additions` で挙げた手書きの doc 読み解き fallback を使う。preset 側に missing 通知 / installer を求めない。

## Dangerous DB / Test Command Category

Rails / Redmine 開発では `test` 用環境変数を付け忘れると development DB を破壊する系の事故が起きやすい。具体的な command と環境変数は project 固有なので preset 側に焼き込まないが、共有 ルールとして次を扱う。

- project に "誤実行で development DB / shared state が壊れる test / db 系 command" がある場合、必須環境変数とその理由を **project-local layer に明記する** (例: `<env-var>=<value> <command>` のセット、リセットせず使うと壊れる shared state、parallel runner 起動時の前提)。マーカー外の preset 説明に頼らず、操作者が AGENTS.md の冒頭近くで気づける位置に置く。
- 偶発的な「重い・遅い」では済まず DB / state を破壊する系の command は警告だけでは不十分。実行前 / 実行後の確認 step、復旧 command、復旧コスト見積りも project-local layer 側に記録する。preset 側はそのカテゴリの存在だけを伝える。
- preset 側は project 固有の DB 再生成 script、log capture script、parallel test runner、JS test runner、project 固有 lint といった具体的な command 名を必須要件として列挙しない。これらは project ごとに名前と存在有無が違うため、必須化すると別 project で誤誘導になる。

## Presenter / YAML / Doc-Readonly Category

project が Rails 標準を超える表示層やルールを採用している場合、preset 側はカテゴリだけを認識し、具体 path を列挙しない。project-local layer 側で次を明記する。

- **Presenter / decorator / form object / service object** 等を採用しているか。採用している場合の保管 directory と、各層に乗せてよい責務 / 乗せてはいけない責務 (例: controller 肥大化禁止、view helper への business logic 混入禁止、presenter concerns への業務ロジック混入禁止)。
- **設定 YAML / 画面定義 YAML** (`config/...` 配下など) を使っているか。使っている場合の保管 directory、命名規則、生成物 / 手書き mix のルール。
- **Read-only documentation area** (仕様書 directory、外部設計書 directory、生成物 directory) が存在するか。存在する場合の編集禁止 path と、読み取り専用扱いの根拠。
- これらは project ごとに有無と path が違うため、project-local layer マーカー内へ書く。preset 側は「該当 area があれば project-local layer に書く」というカテゴリ要件だけ持つ。

## Project Tooling / Local Skill / Role-Boundary Override Category

- project が **local skill / sync command** (project root の skill copy を mirror で揃える系の helper) を持つ場合、その同期方法と check command を project-local layer に書く。手編集禁止 / generator 経由必須のルールがあれば併記する。
- project が **role-boundary の local override** (例: 「ガードレール / agent 運用文書 / local skill だけは Codex 直接編集可」「特定 directory 配下は Codex 監査必須」) を採用している場合、その override の範囲と発動条件を project-local layer に明記する。preset 側の標準 split (Claude implements / Codex audits) を override する形で書く。
- project が **private internal tooling、private path、private convention** を持つ場合、対象 path と運用 rule を project-local layer に書く。preset 側に名前を持たない。

## Rails Prohibitions

- 汎用 `redmine` preset に Rails 固有 app path、private docs catalog、controller / model / migration 規約を混ぜ戻さない。
- migration、authorization、data correction を review なしで close しない。
- destructive migration、bulk update、external notification、production operation を owner approval / project operation rule なしに実行しない。
- Redmine journal、commit message、README、logs に credential、token、個人情報、production data excerpt を記録しない。
- project 固有の path / command / 環境変数 / skill / docs catalog を redmine-rails preset 本文に焼き込まない。これらは router の Project-Local Additions マーカー間に書く (preset 側に書くと別 project で誤誘導になる)。
