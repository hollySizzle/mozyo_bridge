# Fallback Retirement Ledger

Redmine #12000。compat / fallback / legacy rail を「残っていること自体」ではなく、
退役条件つきの管理対象として扱うための repo-local 台帳。

この文書は runtime behavior を変更しない。既存 doc / code に散っている
compatibility rail を一覧化し、owner、source of truth、removal condition、
minimum release window、current risk を監査可能にする。

## Purpose

`mozyo-bridge` は dogfooding の速度を優先して、短期互換 rail や fallback を
いくつか残してきた。fallback はそれ自体が悪ではないが、退役条件が無いまま増える
と、次の負債になる。

- 新しい source of truth がどれか分からなくなる。
- fallback が authority のように扱われる。
- old path を消せる時期が判断できない。
- private / operator 固有 policy が OSS default に混入する。

したがって、新規 fallback を追加または維持する場合は、本台帳に少なくとも次を
記録する。

- owner
- source of truth
- safety condition
- removal condition
- minimum release window
- current risk
- status

退役条件をまだ定義できない場合は、空欄にせず
`no_retirement_condition_yet` と明記する。

## Status Vocabulary

- `active_compat`: 現行互換 rail。退役条件または release window を持つ。
- `explicit_fallback`: operator が明示選択する fallback。default path ではない。
- `deprecated_cleanup`: 新規利用禁止で、既存 caller cleanup 待ち。
- `no_retirement_condition_yet`: 技術的または運用上、退役条件がまだ定義されていない。
- `candidate_for_stronger_warning`: 即時削除ではなく warning 強化の候補。

## Ledger

### workspace-anchor legacy read fallback

- owner: workspace identity
- source of truth: `workspace-anchor-project-defaults-migration.md`,
  `workspace-registry.md`
- fallback: `.mozyo-bridge/workspace.json` を旧 anchor 名として読む。
- safety condition: 新名 `.mozyo-bridge/workspace-anchor.json` が authoritative。
  mutating command は both-exist を fail closed し、silent merge しない。
- removal condition: downstream scaffolded repos の migration path が文書化され、
  少なくとも 1 release cycle 以上、新名 write / old read warning が配布された後に
  owner が削除可否を再判断する。
- minimum release window: `>= 1 release cycle after migration path is shipped`
- current risk: old name が「workspace 全状態の正本」に見える。
- status: `active_compat`

### project-defaults legacy read fallback

- owner: workspace defaults renderer
- source of truth: `workspace-anchor-project-defaults-migration.md`,
  `workspace-defaults-renderer.md`
- fallback: `.mozyo-bridge/workspace-defaults.yaml` を旧 defaults 名として読む。
- safety condition: 新名 `.mozyo-bridge/project-defaults.yaml` が authoritative。
  新規 write は新名のみ。both-exist は mutating command で fail closed。
- removal condition: old-only workspace の migration route が配布され、
  generated snippet / docs / scaffold が新名に揃った状態で少なくとも 1 release
  cycle を経た後、usage inventory を確認して再判断する。
- minimum release window: `>= 1 release cycle after migration path is shipped`
- current risk: old name が runtime workspace state の置き場に見える。
- status: `active_compat`

### notify legacy task queue

- owner: handoff transport
- source of truth: `tmux-send-safety-contract.md`
- fallback: `notify-codex-legacy-task` / `notify-claude-legacy-task` が旧
  `.agent_handoff/tasks.json` queue を読む。
- safety condition: 新規 caller は標準 `handoff send` または標準 `notify-*` を使う。
  legacy queue は durable anchor を CLI 引数として持てないため、standard path の
  代替正本にしない。
- removal condition: repo 内 caller と documented operator runbook の inventory
  が zero になり、少なくとも 1 release cycle の deprecation note を経た後。
- minimum release window: `>= 1 release cycle after zero-caller inventory`
- current risk: legacy queue を standard handoff の代替として誤用すると、durable
  anchor / review gate の境界が薄くなる。
- status: `deprecated_cleanup`

### notify wrapper compatibility lines

- owner: handoff transport
- source of truth: `tmux-send-safety-contract.md`
- fallback: 標準 `notify-*` が成功時に `notified <agent>: journal=...` の legacy
  line を併出する。
- safety condition: 契約上の正本は `DeliveryOutcome` と delivery record。
  legacy line は external script / smoke 用の courtesy に限定する。
- removal condition: downstream scripts / smoke / docs が structured outcome を読む
  ことを確認し、legacy line 依存 caller が zero になった後。
- minimum release window: `>= 1 release cycle after structured-output-only docs`
- current risk: human / script が legacy line を成功正本と誤認する。
- status: `active_compat`

### strict standard send rail

- owner: handoff transport
- source of truth: `tmux-send-safety-contract.md`
- fallback: `--mode standard` は queue-enter default に対する strict explicit fallback。
- safety condition: default ではない。marker observation を Enter の必要条件にし、
  timeout では rollback する。strict rail を弱化しない。
- removal condition: `no_retirement_condition_yet`
- minimum release window: `no_retirement_condition_yet`
- current risk: default と誤解されると marker_timeout 再試行が増える。ただし
  regression / observability には必要。
- status: `explicit_fallback`

### tmux rendered-text completion observation

- owner: receiver-state observability
- source of truth: `ack-completion-receiver-state.md`,
  `tmux-send-safety-contract.md`
- fallback: tmux `capture-pane` / rendered text / pane silence から completion を推測する。
- safety condition: completion truth ではなく short-term fallback。authoritative な
  completion / ack は durable record または将来の machine-readable signal から導出する。
- removal condition: sidecar / control-event / `mozyo_bridge_pty` 由来の
  machine-readable receiver signal が実装され、cockpit / audit / send flow がそれを
  読むようになった後。
- minimum release window: `no_retirement_condition_yet`
- current risk: pane silence や scrollback shape を completion と誤認する。
- status: `no_retirement_condition_yet`

### low-level pane operation commands

- owner: operator debug surface
- source of truth: `tmux-send-safety-contract.md`
- fallback: `mozyo-bridge read` / `message` / `type` / `keys`、
  `message --no-submit`。
- safety condition: operator/debug 用。standard handoff / reply の代替にしない。
  durable record と owner / review gate の正本性は Redmine journal 側に置く。
- removal condition: `no_retirement_condition_yet`
- minimum release window: `no_retirement_condition_yet`
- current risk: emergency path が通常 workflow に戻り、delivery / ack contract を
  bypass する。
- status: `explicit_fallback`

### normal-window role fallback

- owner: target discovery
- source of truth: `pane-centric-cockpit-semantics.md`
- fallback: normal local `mozyo` session では window name `claude` / `codex` を
  role fallback として使い、`role_source=window_name` と明示する。
- safety condition: cockpit では window name を role identity にしない。handoff は
  pane user option / registry / cwd / process preflight を通す。
- removal condition: normal local session でも pane user options が十分に普及し、
  old session / window-name-only session の inventory が zero になった後。
- minimum release window: `no_retirement_condition_yet`
- current risk: cockpit と local session の意味差分が hidden fallback になる。
- status: `active_compat`

### display prefix / session-window naming projection

- owner: presentation layer
- source of truth: `pane-centric-cockpit-semantics.md`,
  `iterm-webviewer-presentation-boundary.md`
- fallback: tmux session prefix、window name、pane title、border label、iTerm / VS Code
  label を discovery hint として見る。
- safety condition: display label は projection only。prefix だけで managed / safe /
  targetable と判定しない。private path や secret-shaped value を出さない。
- removal condition: `no_retirement_condition_yet`
- minimum release window: `no_retirement_condition_yet`
- current risk: UI 表示が identity authority に昇格すると、iTerm2 / VS Code / 手動
  tmux 操作で routing が壊れる。
- status: `no_retirement_condition_yet`

### Claude permission mode override rail

- owner: managed Claude pane launch
- source of truth: `cockpit-sublane-operating-model.md`
- fallback: `MOZYO_CLAUDE_PERMISSION_MODE` による Claude permission mode override。
- safety condition: default policy を壊さない opt-in / explicit override として扱う。
  private operator 固有の Claude mode policy を OSS default に焼き込まない。
- removal condition: Claude CLI 側の stable documented launch flag / config contract と
  mozyo managed-pane policy が揃い、env override なしで再現可能になった後。
- minimum release window: `no_retirement_condition_yet`
- current risk: environment dependent な auto-mode 起動が再現性を下げる。
- status: `active_compat`

### bootstrap curl fallback

- owner: bootstrap / distribution
- source of truth: `bootstrap.md`, `skill-distribution.md`
- fallback: plugin marketplace / packaged distribution が primary になった後も、
  curl-based bootstrap を legacy fallback として説明する場合がある。
- safety condition: curl route は operator が明示的に選ぶ recovery / legacy path。
  権限変更、install、外部送信は operator approval と release doc の gate に従う。
- removal condition: packaged distribution と plugin marketplace route が release docs
  と smoke で十分に安定し、curl route の support 必要性がなくなった後。
- minimum release window: `no_retirement_condition_yet`
- current risk: bootstrap path が複数あると、どれが supported default か曖昧になる。
- status: `candidate_for_stronger_warning`

### OTel pre-injection unknown sessions

- owner: runtime observability
- source of truth: `otel-event-store.md`, `session-inventory.md`
- fallback: OTel / event injection 前に起動していた session は、restart まで unknown /
  legacy-shaped として扱う。
- safety condition: unknown session を healthy と断定しない。routing safety は live
  target preflight と registry / pane marker に置く。
- removal condition: event injection が normal init / adopt / append path に十分普及し、
  old sessions の restart window を経た後。
- minimum release window: `no_retirement_condition_yet`
- current risk: observability が無いことを正常状態と混同する。
- status: `no_retirement_condition_yet`

## Change Policy

fallback を追加または維持する変更は、次を同一 issue または関連 issue に残す。

1. 本台帳への entry 追加または更新。
2. source of truth 文書への link。
3. default path か explicit fallback かの分類。
4. authority ではない場合、その旨の明記。
5. removal condition。未定なら `no_retirement_condition_yet`。
6. release note / migration note が必要かの判断。

fallback が runtime safety、credential、release、workflow、scaffold、skill / plugin
surface に触れる場合は、通常の guardrail に従って per-task Codex review または
必要な Design Consultation を行う。

## Retirement Process

fallback を削除または warning 強化する場合は、次の順序を守る。

1. caller / docs / scaffold / skills / plugin mirror の inventory を取る。
2. source of truth がどこへ移るかを明記する。
3. operator に必要な migration command または runbook を出す。
4. minimum release window を満たしたことを Redmine に記録する。
5. 削除 commit では tests / docs / generated outputs を同時に同期する。

削除の理由が「古いから」だけでは不十分である。fallback の存在が具体的に
safety / source-of-truth / maintenance risk を生んでおり、かつ migration route が
あることを条件にする。

## Immediate Follow-up Candidates

- `notify-*-legacy-task` の caller inventory を取り、zero なら warning 強化または
  removal task を切る。
- workspace anchor / project defaults の旧名 fallback は、次 release 後に
  migration command / warning 強化の要否を再評価する。
- tmux rendered-text fallback は短期に消さず、machine-readable receiver signal の
  設計と紐づける。
- normal-window role fallback は cockpit / local session の差分吸収として当面残すが、
  `role_source=window_name` の露出を維持する。
