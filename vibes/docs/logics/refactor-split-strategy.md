# Refactor Split Strategy

Redmine #12002。`commands.py` / `cli.py` / `tests/test_mozyo_bridge.py` を一括で
綺麗にしようとせず、次に触る機能単位で安全に分割するための設計正本。

この文書は docs-only の方針であり、実装差分を含まない。

## Current Measurement

2026-06-15 時点の実測:

```text
src/mozyo_bridge/application/commands.py   5014 lines
src/mozyo_bridge/application/cli.py        2240 lines
tests/test_mozyo_bridge.py                18104 lines
```

`commands.py` は少なくとも次の family を同居させている。

- agents list / targets / attention projection
- tmux-ui install / status
- message / read / type / keys
- agent launch / init / status / doctor
- cockpit layout / append / adopt / reset
- notify / handoff send / reply / cross-workspace consult
- scaffold / rules / docs catalog
- otel / events / session inventory
- workspace register / list / inspect / defaults

`cli.py` は top-level parser と全 subcommand registration を 1 function
`build_parser()` に集約している。parser wiring と command handler import が近すぎる。

`tests/test_mozyo_bridge.py` は既に一部の新規 feature test が別 file に移動している
にもかかわらず、古い巨大 test spine として残っている。独立済みの例:

- `tests/test_cockpit_*.py`
- `tests/test_attention_*.py`
- `tests/test_event_timeline.py`
- `tests/test_workspace_registry.py`
- `tests/test_rename_compat.py`
- `tests/test_redmine_context.py`

つまり、分割は「未着手」ではない。途中で止まった移行を、方針を決めて再開する
状態である。

## Decision

一括リファクタは禁止する。分割は feature family 単位で、次に触る変更の直前に行う。

原則:

1. Behavior-preserving move を先に行う。
2. move commit と behavior change commit を分ける。
3. test split は code split より先に行えるなら先に行う。
4. public CLI output / parser behavior は characterization test で pin してから触る。
5. tmux / handoff / cockpit の safety-critical family は最後に切る。
6. adapter boundary は #12001 の built-in adapter 分類に従う。

## Target Layers

### CLI parser layer

責務:

- argparse parser construction
- option group / subparser registration
- `func` への dispatch binding
- `--help` / choices / default value の public CLI surface

非責務:

- command execution
- tmux IO
- Redmine / ticket semantics
- workspace registry mutation

分割候補:

```text
application/cli.py                         # main(), build_parser() shell
application/cli_agents.py                  # agents / attention parser
application/cli_cockpit.py                 # layout / cockpit parser
application/cli_handoff.py                 # notify / handoff / message parser
application/cli_docs_scaffold.py           # docs / scaffold / rules parser
application/cli_runtime.py                 # otel / events / session parser
application/cli_workspace.py               # workspace / defaults parser
application/cli_release.py                 # release parser (if not already isolated)
```

最初の実装では package shape を固定しすぎない。必要なら `application/cli_parts/`
package にする。重要なのは parser registration を family ごとに分け、top-level
`build_parser()` を registry composition へ寄せることである。

Characterization:

- representative `--help` output snapshots or exact critical substrings
- subcommand choices / defaults / `dest` / `func` binding
- deprecated alias warning behavior
- `main()` の exit code behavior

### Command handler layer

責務:

- argparse `Namespace` を受け取り、application service / domain function を呼ぶ。
- stdout / stderr / JSON output shape を維持する。
- user-facing error を組み立てる。

非責務:

- low-level tmux command execution
- ticket provider API details
- pure domain derivation
- file storage implementation details

分割候補:

```text
application/commands.py                    # compatibility facade, imports old names
application/commands_agents.py
application/commands_cockpit.py
application/commands_handoff.py
application/commands_docs_scaffold.py
application/commands_runtime.py
application/commands_workspace.py
```

最初は facade を残して old imports を壊さない。tests / downstream が
`mozyo_bridge.application.commands.cmd_*` を patch しているため、いきなり import path
を変えると test が壊れる。移動後もしばらく facade から re-export し、retirement は
別 issue で台帳化する。

### Domain orchestration layer

責務:

- pure decision / plan / derivation
- provider-independent records
- fail-safe state derivation

候補:

- `derive_attention`
- cockpit plan / adopt / reset assessment
- handoff delivery outcome construction
- workspace lane resolution
- release gate plan

方針:

- pure function は先に domain / application helper へ抜く。
- command handler は I/O と print に寄せる。
- `datetime.now()` / subprocess / tmux / Redmine MCP へ直接触るものは pure layer に入れない。

### Infrastructure IO layer

責務:

- tmux command execution
- file system write
- registry / sqlite access
- provider API call
- subprocess / `gh` / build tool call

方針:

- tmux IO は safety-critical なので、最初のリファクタ対象にしない。
- 既に `infrastructure/tmux_client.py` 等へ切れているものを優先的に使う。
- IO layer は outcome を返し、workflow approval semantics を持たない。

### Ticket / Redmine shaped notification layer

責務:

- durable anchor normalization
- provider-shaped issue / journal / comment vocabulary
- notify wrapper compatibility

方針:

- #12001 の ticket adapter boundary に合わせる。
- Redmine が唯一の provider でも、internal record 名は `IssueRef` / `JournalRef` のように
  provider-neutral に寄せる。
- close approval / review verdict の判断は provider helper ではなく core workflow が持つ。

## Test Split Priority

### Priority 1: low-risk pure / docs / release families

最初に切る候補:

- `ReleaseHelperParserTest`
- `ReleaseCheckTreeTest`
- `ReleaseCheckScaffoldTest`
- `ReleaseCheckArtifactTest`
- `ReleaseCheckWorkflowTest`
- `ReleaseWorkflowRunsTest`
- `ReleaseWorkflowWaitTest`
- `ReleaseBumpPublishParserTest`
- `ReleaseBumpCheckTest`
- `ReleaseBumpToTest`
- `ReleasePublishTest`
- `WorkspaceDefaultsRendererTest`
- `InstructionDoctorTest`
- `InstructionInstallTest`
- docs / catalog / canonical renderer 系 test

理由:

- tmux live behavior と距離がある。
- file / parser / pure output が中心で characterization しやすい。
- release helper contract が既に境界を明文化している。

### Priority 2: scaffold / skill / guardrail families

候補:

- `ScaffoldRulesTest`
- `ScaffoldStatusTest`
- `ScaffoldDiffTest`
- `PluginMarketplaceTest`
- `SkillCrossWorkspaceGuidanceTest`
- `SkillWorkflowSemanticAnchorsTest`
- `CodexAutonomousGuardrailLaneTest`

理由:

- 重要だが、既に generator / drift gate の考え方がある。
- test file split の効果が大きい。
- behavior change と混ざると危険なので move-only commit が必要。

### Priority 3: handoff / notify / message

候補:

- `NotifyContractTest`
- `MessageContractTest`
- `MessageGateGuidanceTest`
- `WaitForTextContractTest`
- `HandoffDomainTest`
- `HandoffCliParserTest`
- `HandoffOrchestratorTest`
- `RelaxedQueueEnterRailTest`
- `DeliveryRecordTest`
- `HandoffRecordEmissionTest`
- cross-workspace handoff tests

理由:

- workflow safety の中核。
- monkeypatch target が `application.commands` に集中しているため facade 方針が必須。
- test split は有用だが、code split は characterization を厚くしてから行う。

### Priority 4: cockpit / tmux runtime

候補:

- remaining `CommandTest`
- pane resolver / agent discovery / session naming / tmux UI
- cockpit layout / adopt / reset residual tests

理由:

- dogfooding 圧力が強いが、手動 tmux / iTerm2 / live layout と絡む。
- pane-centric semantics と desired presentation state の設計が進行中。
- move-only でも patch target / fake tmux の破壊が起きやすい。

## Characterization Strategy

分割前に pin するもの:

- CLI parser:
  - selected `--help` substrings
  - defaults and choices
  - deprecated alias behavior
  - `args.func` binding
- command output:
  - text table headers
  - JSON keys
  - error message stable phrases
  - exit code
- handoff:
  - `DeliveryOutcome` status / reason
  - marker shape
  - rollback / Enter timing contract
  - legacy compatibility line behavior
- docs / scaffold:
  - generated output byte equality
  - drift check recovery command
  - catalog resolve / file convention coverage
- tmux:
  - fake tmux command sequence
  - no mutation on dry-run / preview
  - fail-closed before typing

Characterization tests should describe existing behavior, not ideal behavior. If existing
behavior is wrong, first pin the current behavior in the move commit, then change behavior in
a separate issue/commit with explicit review.

## Move Commit Rules

1. Move one family at a time.
2. Keep public import compatibility through `commands.py` / `cli.py` facade when tests or
   downstream patch paths rely on it.
3. No logic edits in move commit except import path mechanical changes.
4. Run focused tests for moved family plus full suite when feasible.
5. Commit message must say `move-only` or `behavior-preserving` when true.
6. If a move reveals hidden coupling, stop and record a design consultation instead of
   pushing through.

## First Concrete Sequence

Recommended first sequence for v0.8:

1. Split release helper tests out of `tests/test_mozyo_bridge.py`.
2. Split workspace defaults / docs catalog / canonical renderer tests.
3. Extract CLI release/docs/scaffold parser registration into family modules.
4. Extract command handlers for docs/scaffold/workspace defaults where IO boundaries are
   already narrow.
5. Only after that, revisit handoff / notify / cockpit code split with stronger
   characterization.

This sequence maximizes learning while avoiding the most safety-critical tmux send path.

## Relation To Adapter Boundary

#12001 defines built-in adapter categories. This refactor should serve that design, not
invent a parallel plugin system.

- ticket adapter work maps to handoff / notify / Redmine-shaped command families.
- presentation adapter work maps to agents targets / attention / cockpit display projection.
- runtime adapter work maps to tmux IO and send safety, but should be deferred until the
  command/test split has reduced patch-path coupling.
- catalog backend work should remain docs/catalog governance first, DB/index second.

## Non-Goals

- No arbitrary plugin loading.
- No single mega refactor branch.
- No behavior change hidden inside move commit.
- No removal of legacy import paths without a fallback retirement entry.
- No direct rewrite of handoff / tmux safety while splitting parser or tests.
