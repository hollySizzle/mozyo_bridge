# route identity ledger / live pane re-resolution contract

Redmine #12553 / parent #12499。delegated coordinator / sublane handoff と
callback target を、stale `pane_id` ではなく stable route identity と live pane
inventory の再照合で fail-closed に解決するための spec。

## 背景

#12547 / #12549 / #12550 で、classical oracle、child candidate resolver、planner /
actuator-seam plan layer は review approved になった。一方で、実機 tmux pane は
restart / split replacement / cockpit re-layout で `pane_id` が変わる。handoff /
callback が過去の `pane_id` snapshot を route authority として扱うと、別 pane への誤送信
または stale pane への送信を起こす。

本書は `pane_id` を cache/snapshot に限定し、stable route identity + live inventory
再照合で target を解決する contract を固定する。実機 smoke (#12546) は本書の射程外で、
ここでは classical tests と実装設計の境界だけを置く。

## Authority Model

Route authority は次の stable identity fields の組で表す。

```yaml
route_identity:
  route_id: <stable route id>
  workspace_id: <workspace / project identity>
  lane_id: <lane identity>
  role: <codex | claude | delegated_coordinator | implementation_gateway | implementation_worker>
  pane_name: <logical pane/window label>
  callback_purpose: <delegation_parent | owning_us_coordinator | audit_coordinator | implementation_lane | none>
  last_seen_pane_id: <cache only; never authority>
  observed_at: <inventory observation timestamp | none>
```

固定境界:

- `last_seen_pane_id` は cache / snapshot / diagnostic evidence のみ。handoff /
  callback target の authority には使わない。
- `pane_name` は stable identity の一部だが、それ単独で target を決めない。
  `workspace_id` / `lane_id` / `role` / `pane_name` を live inventory に照合する。
- private pane id、host path、operator-specific cockpit composition は tracked docs /
  tests に焼き込まない。runtime record に実機結果として残す場合も、public/private
  boundary に従う。
- direct cross-project Claude send と hidden subagent は使わない。lane boundary を跨ぐ
  handoff は target lane の Codex gateway route を経由する。

## Live Re-resolution

handoff / callback の直前に、route ledger の stable identity を live pane inventory
へ再照合する。

1. route ledger から `workspace_id` / `lane_id` / `role` / `pane_name` /
   `callback_purpose` を読む。
2. live pane inventory (`agents targets` 相当) を取得する。
3. stable identity fields で候補を絞る。
4. 候補が 1 件だけなら、その候補の current `pane_id` を send target cache として使う。
5. 候補 0 件なら `target_unavailable` で fail-closed。
6. 候補 2 件以上、または inventory row が ambiguous なら `target_ambiguous` で
   fail-closed。
7. `last_seen_pane_id` と current `pane_id` が異なる場合は、stale cache を更新してよいが、
   送信可否は stable identity の一意照合で判断する。

Fail-closed outcome は durable record に残す。少なくとも attempted route identity、
failure reason (`target_unavailable` / `target_ambiguous` / `identity_mismatch`)、
recovery owner、retry anchor を記録する。

## Handoff / Callback Record Fields

handoff / callback の durable record は pane id ではなく route identity を主語にする。

```markdown
## Route identity resolution

- record_kind: route_identity_resolution
- source_issue: <Redmine issue id>
- route_id: <stable route id>
- workspace_id: <workspace identity>
- lane_id: <lane identity>
- role: <receiver role>
- pane_name: <logical pane/window label>
- callback_purpose: <purpose token | none>
- last_seen_pane_id: <cache only | none>
- live_resolution: <resolved | target_unavailable | target_ambiguous | identity_mismatch>
- resolved_pane_id: <runtime evidence only | none>
- inventory_observed_at: <timestamp | none>
- send_outcome: <sent | blocked>
- recovery_anchor: <journal / retry command pointer | none>
```

`resolved_pane_id` を record に残す場合は runtime evidence であり、route authority ではない
ことを narrative で明示する。tracked docs / tests には実機 pane id を固定値として書かない。

## State Boundary

本 spec は既存の 4 層 state model に従う。

- Redmine issue / journal: workflow decision と durable handoff/callback record。
- Registry / route ledger: stable workspace / lane / route identity。
- Live tmux: liveness と current handoff target evidence。
- Inventory projection: live tmux を読んだ candidate table。projection は authority ではない。

DB / static file / pane scrollback のいずれか単独を source of truth にしない。route は
stable identity を authority とし、送信直前だけ live pane id へ解決する。

## Marker Timeout Fallback

route identity resolution が成功した後の send rail は
`vibes/docs/logics/tmux-send-safety-contract.md` に従う。

- `marker_timeout` で strict rail が rollback した場合、標準 retry は自動実行しない。
- 許容 fallback は `mozyo-bridge read <agent>` で read marker を refresh し、
  `mozyo-bridge message --no-submit <agent> "<body>"` で staged input を作り、operator が
  pane で Enter を押す flow。
- `type` / `keys` は low-level operator/debug primitive であり、単独で durable handoff
  delivery を成立させない。緊急 recovery で使った場合も、その事実と責任境界を durable
  record に残し、標準 route として正規化しない。

## Classical Test Expectations

実機 smoke の前に、次を classical tests / fake inventory で検査する。

- cached `last_seen_pane_id` が stale でも stable identity の live re-resolution で
  current pane に解決する。
- candidate 0 件は `target_unavailable`。
- candidate 複数件または ambiguous row は `target_ambiguous`。
- `workspace_id` / `lane_id` / `role` / `pane_name` の mismatch は送信前に blocked。
- route ledger に stable identity が不足する場合は送信前に blocked。
- tracked tests に private pane id / host path / cockpit composition を fixture として固定しない。
- lane boundary を跨ぐ request は target lane Codex gateway へ向かい、foreign Claude へ
  direct send しない。

## Backend-Neutral Live Resolution (#13297)

#13263 j#72594 の「operational default 前の必須線」により、live resolver は tmux pane
inventory と herdr agent inventory の **backend 中立抽象**へ寄せる。実装は
`backend_neutral_resolver.py` (同 bounded context) で、ledger の resolver を fork せず
adapter で包む。

固定境界:

- **tmux 不変**: `backend: tmux` では live `try_pane_lines` row を無変換で
  `resolve_route` へ渡す。tmux path の挙動は byte 不変 (backend 分岐は adapter 層に閉じる)。
- **herdr identity source**: `backend: herdr` では live `agent list` row の **assigned
  name** (mzb1 scheme、#13247) を identity source とする。decode した
  `(workspace_id, lane_id, role)` を stable slot、canonical assigned name を
  `route_label`/`pane_name` 相当の stable label とし、tmux `pane_id` を route authority に
  しない。transient herdr locator は `last_seen_pane_id` と同じ cache/evidence 扱い。
- **foreign agent**: mzb1 scheme でない name (managed slot 外の herdr agent) は inventory
  正規化で除外する。
- **fail-closed 語彙は共有**: 曖昧解決は fail-closed (`target_ambiguous`)、候補 0 は
  `target_unavailable`、stale cache 検出は `route_identity_stale` を両 backend で継承する。
  herdr のみ、single match だが live locator 不在の row は blank target 送信を拒否して
  `route_locator_missing` へ降格する (herdr identity domain の `rebind_missing_locator` と
  parity)。`resolve_route` は tmux path でこれを emit しない (`try_pane_lines` row は常に
  pane id を持つ) ため tmux 不変は保たれる。

herdr slot の `RouteIdentity` は `herdr_route_identity(...)` で構築し、`pane_name` を
deterministic な canonical assigned name に固定する — stable label が slot から drift しない。

### Live executor 配線 + backend 条件化 (#13302)

#13297 は resolver を pure staged seam として出荷し、live handoff path / state_store への
配線を「後続判断」として defer した (#13297 j#72871 / j#72910)。#13302 はその後続判断を、
**live executor core (`DelegationRouteExecutor`) への配線**として実装する。scope 裁定は
#13302 j#72985 (design consultation answer)。

固定境界:

- **配線先は executor seam**: `ExecutionContext` に backend selector (既定 `tmux`) を持たせ、
  `_resolve` / `_resolve_worker` の hop re-resolution を backend 中立 bridge
  (`resolve_for_route_target_neutral`) 経由にする。`backend=tmux` は既存の
  `resolve_for_route_target` と byte 一致 (guard・resolution・record projection いずれも不変)、
  `backend=herdr` は同じ injected snapshot を live `agent list` として再解決する。backend は
  per-execution selector であり per-identity field ではない (ledger identity は backend 非依存)。
- **実 send path は対象外**: `orchestrate_handoff` (`commands.py`) の target authority
  (tmux=`pane_info` / herdr=`resolve_herdr_target`) は本 US で変更しない。実 send path の
  route authority 収束 (特に herdr `resolve_herdr_target` の `(workspace_id, role)` match key を
  ledger の `(workspace_id, lane_id, role, pane_name)` へ寄せる件) は multi-lane herdr routing の
  仕様決定と交差するため、別 US へ切り出す。
- **`route_locator_missing` は backend 条件化 (herdr 限定)**: single match だが live locator 空の
  降格は herdr backend でのみ発火する。tmux backend は malformed row (`id=""`) を含む synthetic
  input でも `resolve_route_neutral(tmux) == resolve_route(...)` を保つ。これにより #13297 j#72871
  の residual (malformed tmux row が `route_locator_missing` へ分岐する件) を、拘束「tmux backend の
  解決結果 byte 不変」として構造的に閉じる。

## 参照正本

- `vibes/docs/logics/unit-target-model.md`
- `vibes/docs/logics/managed-state-model.md`
- `vibes/docs/logics/session-inventory.md`
- `vibes/docs/logics/tmux-send-safety-contract.md`
- `vibes/docs/specs/delegated-coordinator-decision-records.md`
- `vibes/docs/logics/coordinator-sublane-development-flow.md`
- `vibes/docs/rules/public-private-boundary.md`

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
