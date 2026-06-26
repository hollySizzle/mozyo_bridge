# Session boundary UX: 判定・next-session prompt・Claude pane lifecycle

Redmine #12122 (parent UserStory #12113)。Codex / Claude / pane / compact の境界で、次の agent / session が **durable source (Redmine journal + repo root + execution root + target record)** から作業状態を復元でき、Claude pane の reuse / new / orphan / guarded kill 判断を安全に下せるようにする運用正本。

> 本 doc は session boundary UX の **判定モデル + 運用 runbook** の正本である。判定ルール本文はここに固定し、router (AGENTS.md / CLAUDE.md) や skill 入口に複製しない (`### 入口の薄さ`)。helper の挙動は `src/mozyo_bridge/domain/session_boundary.py` と `mozyo-bridge session boundary-prompt` / `session pane-decision` が正本実装である。

## 背景

長時間作業では Codex / Claude 双方の context rot、compact、pane scrollback 依存が handoff 品質を下げる。特に「どの pane が何の作業中か」「次の session に何を渡すか」「この pane を消してよいか」を人間の記憶に頼る運用は UX と governance の両面で弱い。運用哲学は [[logic-coordinator-sublane-development-flow]]、pane / target の事実モデルは [[logic-unit-target-model]]、desired-state event log は [[logic-managed-state-model]]、pane discovery は [[logic-session-inventory]] に置く。本 doc はそれらの上で「境界をどう判定し、何を次へ渡し、pane をどう畳むか」を固定する。

## 正本性 (authority)

```yaml
authority:
  正本: Redmine issue / journal + repo root + execution root + target record
  非正本: tmux window 名 / session 名 / pane scrollback
```

- session boundary の判定も next-session prompt の内容も Claude pane lifecycle の判断も、**durable record から replay できる**ことを要件とする。pane scrollback / window 名 / session 名は authority にしない (parent US #12113 非目標)。
- #12098 の execution-root propagation は nested project recovery の正本であり、本 doc はそれを **regress させない**。execution root は portable な repo-relative pointer で表現し、private absolute path を public/default docs に焼き込まない ([[rule-public-private-boundary]]、#12098 review j#59662)。

## 1. Session boundary 候補の判定

境界候補の signal を 3 family に分ける。family ごとに **取るべき action が違う** ため、ラベルだけでなく分類を固定する。signal 語彙の正本は `session_boundary.SESSION_BOUNDARY_SIGNALS`。

```yaml
boundary_signals:
  scope:        # 作業 context 自体が変わり、次 turn / session が再 anchor すべき
    - active_issue_change      # active issue が変わる
    - parent_scope_change      # 親 US / Feature scope が変わる
    - version_change           # fixed_version が変わる
    - repo_root_change         # repo root が変わる
    - execution_root_change    # execution root (nested project) が変わる (#12098)
    - gate_transition          # Start / Implementation Done / Review / Close 等の gate 到達
  pressure:     # 現 session が劣化しており、強制 compact 前に区切る方が安い
    - context_pressure         # context 圧迫
    - compact_event            # compact 多発
    - large_tool_output        # 大量 tool output
    - pane_ambiguity           # pane / target の曖昧化
  preservation: # 未確定の durable state がある。境界記録は禁じないが reset/kill を禁じる
    - dirty_diff               # 未 commit 差分
    - running_process          # 実行中 process
    - pending_approval         # 承認待ち
    - unrecorded_journal       # 未記録 journal
```

判定:

- **scope / pressure** が一つでも立てば boundary とみなし、境界 journal を記録して next-session prompt を提示する (silent に続行しない)。
- **preservation** signal は、それ単独では「session を切り替える理由」ではないが、立っている間は **reset / kill の前に必ず durable journal へ未確定 state を記録する**。preservation と scope/pressure が同時に立つ場合は「境界 journal を記録 → handoff、ただし state が durable になるまで pane を reset/kill しない」。
- 未知 signal は黙って捨てず error にする (runbook command の typo を「境界なし」と誤判定しないため)。判定 helper は `session_boundary.classify_boundary(signals)`。

## 2. Next-session boundary prompt

区切りが良いタイミングで、Codex は **次 Codex session 用の compact で replayable な prompt** を提示する。内容仕様を固定し、必要 field を欠かさない。

```yaml
boundary_prompt_fields:
  必須:
    - issue (ticket id)
    - issue_subject   # 短い subject。長文なら要約してよい
    - issue_role      # この issue が何をするものか: smoke本番 / pre-smoke再検証 / metadata cleanup / operator decision 等
    - journal (latest anchor journal id)
  portable_pointer:
    - repo            # portable identifier (canonical session name / workspace id)。絶対 path にしない
    - execution_root  # #12098。portable repo-relative pointer。絶対 path は構造化出力にのみ残す
  推奨:
    - parent_issue    # 親 US (child Task の場合)
    - commit          # 最新 commit hash (あれば)
    - target_lane     # lane / branch label
    - gate_state      # 現 gate
    - verification_state
    - residual_risks  # 残リスク (複数行)
    - pending_action + next_actor  # next_actor: owner | claude | codex
    - signals         # 立った boundary signal
```

- prompt 先頭は handoff notification と同じ **durable-anchor 契約**で始める: 「anchor journal を source-of-truth system から読んでから着手せよ。以下は pointer であって新しい authority ではない」。
- issue id だけを提示しない。少なくとも `issue id + short subject + issue_role + parent_issue` を同じ行または隣接行に置き、特に smoke / release / cleanup / readiness のように誤実行が高コストな issue では `not_this_issue` / `non_goals` を明示する。例: `#12650 Post-layout #12546 pre-smoke readiness を再検証する` は #12546 smoke 本番ではなく、#12499 配下の Test であり、#12546 を実行しない。
- auto-generated subject が長すぎる、description の先頭を丸ごと使っている、または issue id だけでは役割が判別できない場合は、次 session prompt を出す前に subject を短く正規化し、clarification journal を残す。読み手が Redmine を開く前に「どの issue が本番実行で、どの issue が前提確認か」を取り違えないことを prompt の品質条件にする。
- **repo は portable identifier (canonical session name) で参照**し、checkout は workspace registry から解決する。pane location からは解決しない。絶対 repo root と execution-root workdir は構造化出力 (`--json`) にのみ載せ、pasteable text には載せない ([[rule-public-private-boundary]])。
- 実装: `mozyo-bridge session boundary-prompt --issue <id> --journal <id> [--parent ...] [--commit ...] [--target-lane ...] [--execution-root <abs>] [--gate ...] [--verification ...] [--residual ... (repeatable)] [--pending-action ...] [--next-actor owner|claude|codex] [--signal <name> (repeatable)] [--json]`。default は pasteable markdown、`--json` は prompt field + `prompt_markdown` + 絶対 `repo_root`。formatter 正本は `session_boundary.build_boundary_prompt`。

## 3. Claude pane lifecycle

Claude は Codex-managed worker として扱い、pane の new / reuse / orphan / kill は明示判断する。default は **new pane allocation 寄り**、kill / discard は **guarded cleanup** とする (parent US #12113 方針)。

```yaml
pane_lifecycle:
  states: [reuse, new, orphan, guarded_kill, blocked]
  default: new            # 境界では fresh pane を割り当てて durable journal から再 anchor
  reuse:                  # 同一 lane の pane のみ。cross-lane pane の reuse は new へ落とす
    条件: same_lane
  orphan:                 # 非破壊。pane は走らせたまま管理を外す。未確定 state を保全できる
    条件: 常に可
  guarded_kill:           # 破壊的。clean かつ owner kill 承認済みのときのみ
    条件:
      - preservation signal が一つも立っていない (dirty_diff / running_process / pending_approval / unrecorded_journal なし)
      - owner_approved_kill (Codex window 経由で収集・durable journal 記録済み)
  blocked:                # kill/discard が上記を満たさない
```

- **kill / discard は preservation signal が一つでも立っていれば blocked**。未確定 state は durable journal に記録して drain させる。lane を進める必要があるなら kill ではなく orphan する (parent US #12113 非目標: 「未 commit 差分・running process・pending approval・未記録 journal がある場合は reset / kill しない」)。
- **kill / discard は owner approval gate 対象**。owner 承認は Codex window 経由で収集し durable journal に記録する。Claude pane で観測した owner の口頭 OK は承認ではない ([[rule-project-agent-workflow]] の `### Claude Owner-Question Bypass Prohibition` / `### Owner Close Approval Delegation`)。
- 実装: `mozyo-bridge session pane-decision --requested reuse|new|orphan|kill|discard [--same-lane] [--dirty-diff] [--running-process] [--pending-approval] [--unrecorded-journal] [--owner-approved-kill] [--json]`。decision が `blocked` のとき exit code 3 を返し、operator の `&&` chain が silent に kill へ進めないようにする。decision 正本は `session_boundary.decide_pane_lifecycle`。

## 維持する境界 (regress させない)

- **cross-lane / cross-session Claude direct boundary を弱めない**。cross-lane は target lane Codex gateway 経由、same-lane Claude direct は既存 guardrail が許す範囲のみ ([[logic-coordinator-sublane-development-flow]]、[[skill-workflow-reference]])。本 doc は pane lifecycle の判断を足すだけで、この routing を変えない。
- **tmux window/session/pane scrollback を authority にしない**。boundary 判定・prompt・lifecycle のいずれも durable record から replay できる形に保つ。
- **#12098 execution-root propagation を維持する**。next-session prompt の execution root は portable pointer で表現し、絶対 path は構造化出力にのみ残す。
- **owner approval なしの pane kill / discard / destructive cleanup を自動化しない**。`guarded_kill` は owner 承認 + clean pane の二条件を満たすときだけ許す。

## 入口の薄さ

判定ルール・prompt 仕様・lifecycle 条件の本文はこの doc に固定する。AGENTS.md / CLAUDE.md / skill 入口には「session boundary は [[logic-coordinator-sublane-development-flow]] と本 doc を読む」という pointer のみ置き、同じ判断材料を複製しない ([[rule-llm-rule-authoring]])。helper 出力 (`session boundary-prompt` / `session pane-decision`) は本 doc の判定を実行可能にしたものであり、doc と実装が drift したら同一 task で両方を直す。
