# Session continuity user harness 仕様

Redmine #13529「新規session移行packageでactive workflowと承認境界をdurableに復元できる」。新規 session が chat memory や pane scrollback に依存せず、現在地を復元するための user harness の構成と session 移行 package を定義する。

## Harness の構成

| layer | 正本 / 役割 | 時点依存 state |
| --- | --- | --- |
| router | `AGENTS.md` / `CLAUDE.md`。tool-specific 入口 | 持たない |
| governance | central preset と `.mozyo-bridge/config.yaml` | role/provider の現運用値だけ config で保持 |
| knowledge | cataloged `vibes/docs/` | 恒久的な rule / logic / spec / task だけ |
| durable work state | Redmine issue / journal | active gate、承認、検証、次 actor |
| transition bundle | `vibes/docs/temps/session-handoff-<issue>.md` | 次 session が消費するまでの pointer 束 |
| formatter | `mozyo-bridge session boundary-prompt` | latest journal を指す pasteable prompt |

優先順位は router が示す命令順位に従う。通知本文と transition bundle はいずれも pointer であり、Redmine source journal を上書きしない。通知の kind / summary と journal が矛盾する場合は journal を採用し、判断を fail-closed にする。

## 文書配置

- `vibes/docs/logics/`: 業務ロジック。
- `vibes/docs/rules/`: プロジェクト規約。
- `vibes/docs/specs/`: 仕様。
- `vibes/docs/tasks/`: 再実行可能な手順書。
- `vibes/docs/temps/`: 一時ドキュメント。恒久参照にせず、消費後に削除する。

進捗 snapshot、lane の live 状態、時限的な承認範囲を rules / logics / specs / tasks に焼かない。恒久文書は discovery と contract に限定し、現在値は Redmine と transition bundle に置く。

## Session 移行 package

境界では次を一組として用意する。

1. active な session-transition US と、その Task / Test / 実在する Bug。
2. Redmine の最新 journal。active chain、承認範囲、preservation signal、次 actor を記録する。
3. `vibes/docs/temps/` の transition bundle。複数 issue の subject・role・latest journal をまとめるが、authority にはしない。
4. `logic-session-boundary` に従う next-session prompt。先頭で source-of-truth journal の再読を命じる。

bundle には host-local absolute path、credential、secret、pane scrollback を含めない。repo は portable identifier、execution root は repo-relative pointer で表す。

## 更新契機

次のいずれかで package を更新する。

- active issue / parent / Version / repo / execution root が変わった。
- Implementation Done / Review / QA / Close など gate が遷移した。
- owner の承認範囲が変わった。
- 新規 session へ移る、または context pressure が高まった。

worker の進行中に snapshot を追記し続けるための polling は行わない。境界作成時に source journal を一度再読し、既知の最新 anchor を記録して turn を終了する。後続 callback があれば、新規 session は必ず Redmine の latest journal を再取得する。

## Acceptance

fresh session は prompt だけを入口として、次を復元できなければならない。

- ticket IDだけでなく subject、issue role、parent、latest journal。
- active work と dependency 順の queued work。
- owner 承認の対象と明示的な除外。
- commit / branch / verification の既知状態。
- preservation signal と、実行してはいけない操作。
- pending action と next actor。

復元後は transition bundle を authority に昇格させず、source journal と cataloged docs で照合する。受領を durable journal に記録した後、stale bundle は削除対象にする。
