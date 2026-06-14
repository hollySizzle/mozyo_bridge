# Cross-Project Cockpit Smoke Runbook

Redmine #11905 / #11911。別 project の Unit を cockpit に載せる時の
generic runbook と smoke 観点を定義する。cockpit は display grouping であり、
routing / governance identity の正本ではない。

本 doc は portable runbook である。どの private project を同じ cockpit group に
載せるか、どの順で並べるか、どの lane policy を使うかは operator / private
consumer 側の運用規約に置く。

## Baseline

現行 CLI の事実:

- `mozyo cockpit` / `mozyo cockpit append` は current workspace を cockpit
  session に create / append / focus する。
- `mozyo cockpit adopt` は既存 normal `mozyo` session の codex / claude pane を
  cockpit column へ移す。`--confirm` なしでは preview のみ。
- `mozyo-bridge agents targets` は candidate listing であり、target selection
  ではない。
- `--target-repo auto` は explicit `%pane` target の cwd から repo identity gate
  を解決する。
- local host と remote SSH host の cockpit boundary は
  `local-remote-cockpit-host-boundary.md` を正本とする。

2026-06-14 時点の local baseline:

```bash
PYTHONPATH=src python3 -m mozyo_bridge agents targets --session mozyo-cockpit
```

は main `mozyo_bridge` lane の Codex / Claude pane のみを表示した。これは現状確認
であり、cross-project append 成功の証明ではない。

## Runbook

### 1. 事前条件

- 追加したい project が mozyo workspace として識別できる。
- その project の Codex / Claude pane は `mozyo-bridge init` または `mozyo cockpit`
  により role / workspace marker を持つ。
- cross-project handoff の durable anchor が Redmine / Asana などに存在する。
- private grouping policy は repo に書かない。必要なら private runbook に置く。
- local / remote SSH host を混ぜる場合は #11817 の host boundary を先に確認する。

### 2. Preview

対象 project root で preview する。

```bash
cd <project-root>
PYTHONPATH=<mozyo_bridge_repo>/src python3 -m mozyo_bridge cockpit --session <cockpit-session> --dry-run
PYTHONPATH=<mozyo_bridge_repo>/src python3 -m mozyo_bridge cockpit --session <cockpit-session> --json
```

既存 normal session を cockpit に移す場合は、まず adopt preview にする。

```bash
cd <project-root>
PYTHONPATH=<mozyo_bridge_repo>/src python3 -m mozyo_bridge cockpit adopt --session <cockpit-session> --json
```

preview が `already in cockpit` / `focus` / `append` / `blocked` のどれかを明示しない
場合は進めない。stale cockpit や unmanaged session は follow-up issue に分割する。

### 3. Append or adopt

新しい column を作る場合:

```bash
cd <project-root>
PYTHONPATH=<mozyo_bridge_repo>/src python3 -m mozyo_bridge cockpit --session <cockpit-session>
```

既存 normal session の live panes を移す場合:

```bash
cd <project-root>
PYTHONPATH=<mozyo_bridge_repo>/src python3 -m mozyo_bridge cockpit adopt --session <cockpit-session> --confirm
```

`adopt --confirm` は pane を移す mutation である。preview で source session /
target cockpit / column が意図通りであることを確認してから実行する。

### 4. Discovery smoke

追加後に coordinator 側で target table を確認する。

```bash
PYTHONPATH=<mozyo_bridge_repo>/src python3 -m mozyo_bridge agents targets --session <cockpit-session>
PYTHONPATH=<mozyo_bridge_repo>/src python3 -m mozyo_bridge agents targets --session <cockpit-session> --json
```

最低限、次を満たす。

- 各 project Unit に Codex / Claude target が見える。
- `WORKSPACE` が project 間で混同していない。
- `LANE` が default / issue lane を区別している。
- `ROLE_SOURCE` は cockpit pane では `pane_option` が primary。
- `VIEW_KIND` は cockpit column では `cockpit_pane`。
- `REPO` / `BRANCH` が operator の期待と合う。
- `AMBIG` が `0`。ambiguous target がある場合は handoff しない。

### 5. Handoff smoke

cross-project handoff は target project の Codex gateway に送る。別 project の
Claude へ direct send しない。

手順:

1. durable anchor を先に作る。
2. `agents targets` で target project の Codex pane を一意に選ぶ。
3. explicit `%pane` と `--target-repo auto` で送る。

```bash
PYTHONPATH=<mozyo_bridge_repo>/src python3 -m mozyo_bridge handoff send \
  --to codex \
  --target %<target-project-codex-pane> \
  --target-repo auto \
  --source redmine \
  --issue <issue-id> \
  --journal <journal-id> \
  --kind <kind>
```

この smoke の成功条件は pane に文字が届くことではない。target Codex が durable
anchor を読み、必要なら自 project の Claude へ same-lane handoff できる状態である
ことを確認する。

## Follow-up Issue Triggers

次を見つけたら、この runbook に混ぜず follow-up issue に切り出す。

- `agents targets` が workspace / lane / role / repo を混同する。
- `AMBIG=1` なのに handoff が進められる。
- cross-project handoff が Claude direct send で運用されている。
- local / remote SSH host の pane が同じ host として扱われる。
- `mozyo cockpit` preview と実行結果が違う。
- `adopt --confirm` の失敗時に pane が orphan になり、復旧手順が不足する。
- width / layout の live geometry 問題が identity / routing と混ざる。
- private project grouping policy を docs / skill / preset に入れたくなる。

## Non-goals

- private cockpit composition を OSS default として定義しない。
- cross-project GUI / WebViewer を本 doc で実装しない。
- worktree add/remove policy を core CLI に入れない。
- Redmine / Asana gate state を mozyo DB に複製しない。

## Acceptance Mapping

- generic runbook: Preview -> append/adopt -> discovery smoke -> handoff smoke。
- smoke 観点: workspace / lane / role / pane / repo / branch / ambiguity / host boundary。
- follow-up 分割: failure trigger を明示。
- #11817 接続: local / remote SSH host boundary を smoke 観点に含める。
