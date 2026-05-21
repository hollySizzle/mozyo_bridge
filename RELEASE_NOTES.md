# リリースノート

このファイルは、各リリースで何が変わったのか、そしてなぜ必要だったのかを人間向けに説明するためのものです。単なるコミット履歴ではなく、プロダクトとしての流れが分かる粒度で書いています。

記載は Git の release commit と利用可能な tag を元にしています。一部の過去バージョンは release commit はありますが、現在の repository には対応する tag がありません。

## Unreleased

次の release に向けて準備中の変更です。version bump や tag 付与は別 release task で扱います。

### 変更点

- 新しい scaffold preset `redmine-rails-governed` を追加しました。`redmine-rails` を継承しつつ、full guardrail governance package を opt-in で被せられます。
- `scaffold apply redmine-rails-governed` は target repo の `.mozyo-bridge/` 配下に rule files と docs catalog skeleton (`catalog.yaml.example`) を配布します。docs catalog tooling (validator / resolver / generator / impact checker) は target repo に vendor copy せず、`mozyo-bridge` package に同梱された `mozyo-bridge docs ...` CLI として提供されます。配布された artifact は scaffold manifest に登録され、`scaffold status` が drift を検出します。
- docs catalog に `coverage_roots` field を追加しました。`mozyo-bridge docs validate --check-file-coverage` は catalog 側 roots を読みつつ、CLI `--coverage-root` が指定された場合は CLI 側が優先されます。
- `scaffold status` の出力ラベルを実態に合わせて `tracked files:` に変更しました。manifest が router 以外の scaffolded artifact も追跡している現状を誤解させない表現にします。
- governed preset に Claude Nagger 設定 skeleton (`.claude-nagger/{config,command_conventions,mcp_conventions}.yaml.example` と `.gitignore`) と、tmux agent window 用の UI 補助 snippet (`.mozyo-bridge/tmux/agent-ui.conf`) を標準で同梱しました。両者は default-on で `scaffold apply` 時に target repo に配布されます。
- 標準導入したくない project 向けに `scaffold apply --skip-nagger` と `--skip-tmux-ui` opt-out flag を追加しました。スキップした category は manifest にも記録されないため、`scaffold status` は引き続き clean を返します。
- `mozyo-bridge doctor` に `claude_nagger` セクションを追加し、`tmux` セクションには tmux UI snippet の設置状態 (`artifact:` 行) を表示するようにしました。default-on の guardrail がどこまで届いているかを 1 command で確認できます。
- 旧 governed scaffold が target repo に vendor copy していた `.mozyo-bridge/tools/*.py` を廃止し、docs catalog tooling を mozyo-bridge package 側の CLI として提供するように移しました。`mozyo-bridge docs validate / resolve / generate-file-conventions / audit-impact` で同等の運用が可能です。再 apply 時は scaffold の outgoing reconcile が旧 vendor copy を `--backup` / `--force` 経由で安全に除去します。
- `redmine-rails-governed` の gate schema、role split、Codex direct edit gate、完了条件を preset の `agent-workflow.md` に統合しました。別配布していた `.mozyo-bridge/rules/development_flow.md` は廃止し、再 apply 時は manifest 管理下の旧 file を `--backup` / `--force` 経由で安全に除去します。

### なぜ必要だったか

`redmine-rails` preset は薄い router を維持するために、強い gate 文言や docs catalog tooling を project-local layer に委ねていました。実運用で full governance を即時に被せたい project にはこの分離が手数になっていたため、opt-in の governed preset として配布できる形にしました。実運用プロジェクトで実績のある guardrail から、固有業務ドメインや固定 path を取り除き、汎用 Rails+Redmine project に被せられる素材だけを残しています。

`coverage_roots` の構造化は、`--coverage-root` CLI option だけだと「どの roots が必須なのか」が project に残らなかった点を補うためです。catalog 側に書くことで、project が必要な root の集合を docs として宣言でき、`--coverage-root` を毎回打たなくても `mozyo-bridge docs validate --check-file-coverage` が project に合った挙動になります。`scaffold status` のラベル変更は、governed preset によって manifest 追跡対象が増えた現状に合わせた表記の修正です。

Claude Nagger と tmux UI snippet は、単なる便利機能ではなく、agent が別 window / 別 role / 別 context で誤動作する事故を減らす実行時 guardrail の一部です。Dev Container や ephemeral home の環境では home 側に依存した後付け手順が抜けやすく、project ごとに導入状況がバラつく問題がありました。default-on で repo-local scaffold に同梱し、`doctor` で導入状態を 1 command で見える化することで、guardrail の入り忘れを減らすのが今回の意図です。既存の host 側設定 (例えば `~/.tmux.conf`) は **触らず**、scaffold は target repo にのみ artifact を置きます。host 側への組み込みは operator が `source-file <repo>/.mozyo-bridge/tmux/agent-ui.conf` を `~/.tmux.conf` に追記するなど明示的に行う前提です。

docs catalog tooling を target repo へ vendor copy する形は、project 固有コードなのか mozyo-bridge runtime なのかが曖昧で、配布・upgrade・drift 管理がいずれも重くなっていました。今回 tooling 本体を mozyo-bridge package に同梱し、`mozyo-bridge docs ...` CLI に寄せることで、target repo には catalog 等の data だけが残り、runtime tool の version は `mozyo-bridge` の install と一致するようになります。既存環境からの移行は `scaffold apply --backup` (または `--force`) で旧 `.mozyo-bridge/tools/*.py` が outgoing reconcile されるため、operator は 1 command で切り替えられます。

`development_flow.md` を別 file として配る設計は、agent が読むべき正本を増やし、`agent-workflow.md` との責務境界を人間にも LLM にも分かりにくくしていました。governed preset では AGENTS.md / CLAUDE.md がまず `agent-workflow.md` を読むため、実行契約もそこに統合し、入口から正本までの経路を単純にしました。

## v0.4.0 - 2026-05-20

v0.4.0 は、Dev Container などの環境で guardrail を失いにくくし、複数 agent window を少し見分けやすくするリリースです。

### 変更点

- `rules install` と `scaffold apply` で、repo-local な guardrail rules store を使えるようにしました。
- repo-local mode の使い方を README と scaffold rules documentation に追加しました。
- tmux の `claude` / `codex` window に、控えめな status color を付けるようにしました。
- `claude` / `codex` の window 名は変更せず、resolver / handoff routing の互換性を維持しています。
- release note を日本語で整備しました。

### なぜ必要だったか

Dev Container や Codespace では、home directory が永続化されないことがあります。その場合、user-global な guardrail store だけに依存すると、container rebuild 後に agent が必要な rules を読めなくなる可能性があります。

repo-local mode は、必要な guardrail を対象 repo の中に置けるようにするためのものです。これにより、workspace を開いた agent が同じ repo 内の rules を参照でき、環境差による立ち上がり失敗を減らせます。

tmux の status color は、運用上の小さな混乱を減らすための変更です。派手な見た目にすることが目的ではなく、`claude` / `codex` / その他 window を一目で少し区別しやすくするために入れました。

## v0.3.0 - 2026-05-19

v0.3.0 はガードレール強化のリリースです。Claude / Codex / Asana / Redmine / Redmine Rails をまたいだ作業引き継ぎと project scaffold を、より安全に繰り返せるようにしました。

### 変更点

- v0.2.1 alpha 系で検証した内容を安定版としてまとめました。
- scaffold の操作を `scaffold diff`、`scaffold apply`、`scaffold status` に整理しました。
- scaffold 済みの `AGENTS.md` / `CLAUDE.md` を再生成するとき、project-local な追記を保持できるようにしました。
- Asana / Redmine / Redmine Rails の preset 境界を明確にしました。
- `queue-enter`、retry、通知成功が意味する範囲について handoff documentation を強化しました。

### なぜ必要だったか

mozyo-bridge は、tmux pane へ通知する小さな道具から、複数 agent の作業ルールを配布する道具へ広がってきました。この段階で問題になるのは、単に command が足りないことではありません。

どの rule が正本なのか、どの handoff 経路が安全なのか、scaffold の再生成で project 固有の知識が消えないか。そうした曖昧さが運用上のリスクになります。

v0.3.0 では、その不確実さを減らすために scaffold と handoff の手順を整理しました。

## v0.2.1 alpha series - 2026-05-17 to 2026-05-19

v0.2.1 alpha 系は、v0.3.0 に入れる運用変更を安定化するための検証期間です。

### 変更点

- `v0.2.1a1`: handoff primitive、retry guidance、ACK boundary、skill distribution rules を文書化しました。
- `v0.2.1a2`: Redmine Rails scaffold guardrails を追加し、TestPyPI dispatch の入力を release workflow に合わせました。
- `v0.2.1a3`: scaffold を `apply` / `diff` 中心に再設計し、Redmine Rails の project-local layer を明確にしました。
- `v0.2.1a4`: Redmine の review と close approval を分離しました。
- `v0.2.1a5`: stable release 前に残っていた `queue-enter` documentation gap を解消しました。

### なぜ必要だったか

この alpha 系の目的は、機能をむやみに増やすことではありません。handoff をどう送るか、scaffold をどう更新するか、review と close approval をどう分けるかという曖昧さを減らすことでした。

安定版として出す前に、運用ルールの表現と CLI の動きを揃える必要がありました。

## v0.2.0 - 2026-05-14

v0.2.0 では、現在の handoff model の土台を入れました。

### 変更点

- `handoff send` / `handoff reply` を高レベルの通知 primitive として追加しました。
- handoff の結果を後から追える delivery record を追加しました。
- tmux send safety contract を定義しました。
- relaxed `queue-enter` rail と deterministic preflight checks を追加しました。
- release helper commands と release verification docs を追加しました。
- LLM-first bootstrap guide を追加しました。

### なぜ必要だったか

以前は、通知を低レベル command の組み合わせで送ることができました。柔軟ではありますが、operator や agent ごとに挙動がぶれやすい状態でもありました。

v0.2.0 では、handoff を単一の標準経路に寄せました。これにより、失敗したときの説明や監査がしやすくなりました。

## v0.1.13 - 2026-05-13

v0.1.13 では、tmux の agent identity を window model へ移行しました。

### 変更点

- pane label を runtime identity として使う設計から移行しました。
- window-only tmux model に合わせて scaffold docs を更新しました。
- legacy pane-split tmux commands を廃止しました。
- 最初の handoff / reply notification primitive を追加しました。

### なぜ必要だったか

pane label は、実際の tmux session では曖昧になりやすいものでした。agent ごとに `claude` / `codex` window を持つ方が、resolver にも operator にも分かりやすくなります。

## v0.1.12 - 2026-05-13

### 変更点

- tmux の pane capture で message marker が折り返された場合にも扱えるようにしました。

### なぜ必要だったか

長い通知は tmux 上で折り返されます。marker detection が折り返しで失敗すると、通知が期待した場所に届いたかを誤判定する可能性があります。この release では、その経路の信頼性を上げました。

## v0.1.11 - 2026-05-13

### 変更点

- scaffold 済み handoff guidance の表現を調整しました。
- bare `mozyo` startup branch の regression tests を追加しました。

### なぜ必要だったか

bare `mozyo` が repo session を始める標準導線になったため、docs と tests もその導線に合わせる必要がありました。

## v0.1.10 - 2026-05-13

### 変更点

- bare `mozyo` を標準の tmux entrypoint にしました。

### なぜ必要だったか

Claude / Codex の pair を始めるために、複数の setup command を覚える必要がある状態は扱いにくいものでした。`mozyo` だけで repo-scoped session と window を用意できるようにしました。

## v0.1.9 - 2026-05-12

### 変更点

- audit-owned commit workflow guidance を追加しました。
- `mozyo-bridge-agent` plugin を package 化しました。
- `doctor` が Claude plugin-managed skill install を認識できるようにしました。
- default config file が無い場合の tmux startup behavior を改善しました。
- repo-aware `open-here` behavior を追加しました。

### なぜ必要だったか

導入直後や日常運用で、CLI / rules / skills / scaffold / tmux が健康な状態かを個別に確認するのは手間がかかります。`doctor` と plugin-aware な診断により、setup 状態を確認しやすくしました。

## v0.1.7 and v0.1.8 - 2026-05-11

### 変更点

- TestPyPI turnkey acceptance のために CLI version を調整しました。
- `doctor` environment readiness diagnosis を追加しました。
- fresh tester / turnkey acceptance flow を文書化しました。
- tmux handoff message を default で submit するようにしました。
- scaffold rule path を portable にしました。

### なぜ必要だったか

作者以外の環境で install / acceptance smoke を行う準備が必要でした。install し、環境を確認し、target project に scaffold し、結果を検証する流れを再現可能にするための変更です。

## v0.1.5 and v0.1.6 - 2026-05-10

### 変更点

- Asana scaffold preset に role-boundary guardrails を追加しました。
- workflow verification における Codex role boundary を明確にしました。
- `--version` flag を追加しました。
- scaffold rules の default target を current working directory にしました。
- 命令形の文言だけで Codex が policy / skill / rule files を直接編集できる、という誤解を防ぐ guard を追加しました。

### なぜ必要だったか

user request、durable task state、agent authority を分ける必要がありました。軽い依頼文が、ルール変更の直接許可として扱われると危険だからです。

## v0.1.4 - 2026-05-10

### 変更点

- ticket-system scaffold rules を追加しました。
- scaffold home path handling を修正しました。
- 日本語 scaffold routers と dogfood preset を追加しました。
- release / handoff rules を文書化しました。
- Asana preset escalation policy を追加しました。

### なぜ必要だったか

mozyo-bridge を 1 repository の中だけで使う段階から、複数 project に薄い router を配布する段階へ進める必要がありました。central rules を一箇所に置きつつ、target project には入口だけを置くための基礎です。

## v0.1.3 and earlier - 2026-05-09 to 2026-05-10

初期 release では、公開 package と agent 向け documentation の土台を作りました。

### 変更点

- PyPI / TestPyPI publishing workflows を準備しました。
- Asana-driven agent documentation router を追加しました。
- Claude / Codex 用の `mozyo-bridge` skills を追加しました。
- public release 前に documentation を sanitize しました。
- public GitHub install path と Claude skill usage path を追加しました。
- user / agent documentation を整理しました。

### なぜ必要だったか

最初の目的は配布可能にすることでした。より高度な scaffold や handoff を積み上げる前に、install でき、説明でき、安全に公開できる状態にする必要がありました。
