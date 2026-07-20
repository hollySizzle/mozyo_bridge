# herdr-native identity と target 解決 (Redmine #13261)

純 herdr セッション (tmux server なし / `TMUX` 未設定 / 隔離 socket) で `mozyo-bridge`
の registry / pane 解決と handoff target 解決を成立させるための contract。tmux pane
user-options を projection の唯一の source にしていた #13253 の target 識別を、herdr の
**assigned name (durable identity)** + **live inventory (`agent list`)** + **launch-time
sender env** に置き換える。

この doc は `spec-route-identity-ledger` (tmux 側 route identity contract) の herdr 対応版
であり、その fail-closed 姿勢 (pane id は cache/snapshot のみ、authority は stable identity)
を踏襲する。

## 1. Identity model

- **durable identity** = herdr **assigned name** の `mzb1_<workspace>_<role>_<lane>` scheme
  (`domain/herdr_identity.py` の `encode_assigned_name` / `decode_assigned_name`, Redmine
  #13247)。PoC #13175 E10 実測で `agent rename` 付与名は `server stop`/restart を越えて永続。
  - `workspace` = mozyo workspace_id (registry / anchor が持つ workspace identity)。
    **linked git worktree (sublane lane checkout) も main checkout の registry identity を
    継承した同じ project workspace_id を使う (Redmine #13377 / design j#73613, shared
    project workspace model)。** lane の識別は `workspace` segment ではなく `lane` segment
    が担い、lane の slot は `mzb1_<project-ws>_<role>_<lane_label>` として project identity
    を保つ。herdr 上の**配置**は Redmine #13380 (dedicated sublane host workspace) で分離:
    coordinator pair (default lane) は project workspace に、全 lane slot は単一の **sublane
    host workspace** に着地し、herdr workspace 数は「project 1 + host 1」の定数 (lane 数に
    比例しない)。**Redmine #13411 はこの host workspace 内をさらに lane=tab で細分化する:
    非 default lane ごとに専用 herdr tab を割り当て、gateway + worker を同 tab 内 split pair
    として並置する (`herdr tab create` + `agent start --tab [--split right]`)。tab join は
    live inventory の `tab_id` のみを authority とし (label は cosmetic)、fresh lane は tab を
    mint、heal は生存 slot の `tab_id` を読んで同一 tab へ復帰する。** identity model はこの
    配置分離・細分化で変わらない (mzb1 名は workspace segment に project identity を持ち続け、
    tab は placement のみ)。判別は git topology (`_is_linked_worktree`) +
    `_main_worktree_root` 経由の main identity 読みで行い、その継承 precedence は canonical
    worktree inheritance (`_inherited_worktree_result` / `resolve_canonical_session`) と同じ
    **main registry row → main anchor** である (#13152 / #13595 R1-F1。旧実装は main anchor
    のみ読み、registry-only main を fail-closed していた)。registry schema は
    無変更。mint (§5) と全 resolve 側 (send / retire / projection) は単一 resolver
    `herdr_workspace_segment` を共有する (mint == resolve; registry-only main の継承もこの
    共有 resolver で一致する)。placement 判断 (workspace / tab) の pure core は
    sibling module `herdr_lane_topology` (`_launch_target_for_lane` / `_tab_target_for_lane`)。
    - **legacy (correction history): #13331 j#73357 の per-lane workspace token
      (`derive_lane_workspace_token`, `wt_<hash>`)。** 2026-07-07〜08 の移行期に linked
      worktree lane を独自 herdr workspace として立て、その canonical path hash を
      `workspace` segment に使っていた。#13377 の owner 裁定 (#13081 / #13377 description)
      で shared model に置換され、新規 lane はこの token を mint しない。token は
      (a) 既存 legacy lane rows の互換 resolve / retire (`is_lane_workspace_token` 判定、
      default-lane pair を lane として読む compatibility read) と、(b) lane metadata
      record の安定 per-worktree join key、としてのみ残る。
  - `role` = agent kind / runtime provider token = `claude` / `codex` (tmux 側 role resolver の
    `agent_kind` と同一語彙)。mzb1 の "role" field は **workflow role ではなく provider** である。
  - `lane` = checkout-local lane id (未設定は `default`)。
- **transient locator** = herdr の `pane_id` (`agent list` row の `pane_id`、alias `pane` /
  `location`)。per-process 再生成される使い捨て値であり、**identity として persist しない**。
  target への「今」の到達には `rebind_by_name` で live snapshot から都度復元する。
- **workspace_registry は無変更 (#11425)。** herdr anchor に新 schema は足さない。純 herdr
  session の identity anchor は既存の `workspace_id` (registry / `.mozyo-bridge/workspace-anchor.json`)
  + assigned-name scheme で完結する。registry には runtime/pane state を持ち込まない不変条件を維持する。

## 2. Sender self-identity (launch-time env, PRIMARY path)

session-start helper (§5) が launch 時に sender agent process の環境へ自己識別を注入する。
resolver はこれを fail-closed に読む。

- env var (terminal-runtime domain, `domain/herdr_target_resolution.py`):
  - `MOZYO_WORKSPACE_ID` — sender の workspace_id。
  - `MOZYO_AGENT_ROLE` — sender の provider token (`claude` / `codex`)。
  - `MOZYO_LANE_ID` — sender の lane id (空 → `default`)。
- fail-closed rule (`resolve_sender_identity`):
  - `MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` が欠落 / 空 → die (`missing_sender_env`)。
  - `MOZYO_AGENT_ROLE` が `claude` / `codex` 以外 → die (`invalid_sender_role`)。
  - repo anchor (`read_anchor`) が読めない / workspace_id を持たない → die (`missing_anchor`)。
  - env の `MOZYO_WORKSPACE_ID` が anchor の `workspace_id` と不一致 → die
    (`env_anchor_workspace_mismatch`)。checkout を跨いだ env leak で別 workspace の名前を
    mint するのを防ぐ。
- **sender env は target authority ではない。** sender env は (a) coordinator pseudo-target の
  provider 解決の workspace scope、(b) lane context の 2 用途に限る。target の identity は必ず
  live inventory + assigned-name decode で決める (§3)。
- **env なし operator shell は lane-dispatch origin ではない (#13397 finding 2、design consultation
  answer j#73755 = Option B)。** MOZYO_* env を持たない素の operator terminal から
  `handoff send --target-lane` / explicit `--target` を撃つと `resolve_sender_identity` が
  `missing_sender_env` で fail-closed し、send は拒否される。これは意図された境界であり、operator
  shell を新 dispatch origin として admit すると workspace/lane scope + coordinator-binding の
  attestation を迂回する別 route authority を増やすため採らない。正規の lane-dispatch route は
  **coordinator agent → target-lane Codex gateway → same-lane Claude worker** (skill
  `references/workflow.md` `## 同一レーン Claude dispatch` / `## Sublane の coordinator callback`)。
  error 文言はこの herdr-native な原因と正規 route を明示する (tmux 世代の `target_unavailable`
  文言に留めない)。operator が lane を直接叩く必要がある debug 時は attested lane agent pane から
  実行する。
- **agent identity と command-shell attestation は別に測る (Redmine #13614)。** live inventoryに
  `mzb1_<workspace>_<role>_<lane>` agentが存在することや、TUI launch時にenvを注入した事実だけでは、
  そのagentが使うtool-exec subprocessに同じenvが伝播した証明にならない。handoff直前の実command
  contextで3変数とrepo anchorを照合し、`present | missing | conflict`を記録する。`missing` / `conflict`
  は標準sendをfail-closedにし、手動env注入でattestationを捏造しない。これはenv-less operator shellを
  authority化しない既存境界の明確化であり、route authorityを増やさない。

- **startup self-attestation は launch-time の自己観測であり cryptographic attestation ではない
  (Redmine #13637, Design Answer j#76462)。** herdr は `agent get/list/pane/read/target` しか露出せず、
  **launched process の environment を read-only で返す surface を持たない**。稼働中 process の environ は
  外部プロセスから変更不可 (POSIX) なので、env-less な live agent を launcher / doctor が **in-place で
  read も repair もできない** (修復は relaunch のみ)。したがって triplet が実際に spawn 先へ届いたかを
  観測できるのは **agent 自身の process だけ**である。managed launch は provider を
  `mozyo-bridge herdr agent-attest` で wrap し、agent boot 時に自 `os.environ` を launcher が期待する
  identity と照合して `present | missing | conflict` を判定し、**live locator に generation-bind した
  durable record** (home-scoped `herdr-identity-attestation.sqlite`、runtime observation projection、
  env 値・secret は保存しない) を書いてから provider を `exec` する。この record は
  (a) adopt が live name-match を採用してよいかの gate (§5) と、(b) doctor が env-less/mismatch managed
  slot を non-green にする join、の入力になる。record は「今 env を live read した」ことを主張せず、
  boot 時 self-attestation の有無・世代一致・verdict のみを表す。**§2 冒頭の #13614 command-shell
  attestation を置換しない**: startup record は TUI process env の boot 時観測であり、tool-exec
  subprocess への伝播を証明しないため、send 直前の `resolve_sender_identity(os.environ, ...)` は hard
  gate のまま残る (env-less shell は依然 `missing_sender_env` で fail-closed)。record 不在/世代不一致
  (stale)/`missing`/`conflict` の adopt は fail-closed し、**owner 承認の close + same-slot relaunch** を
  next action として返す (自動 destructive repair を行わない)。真の暗号学的 attestation
  (nonce / challenge-response) の導入は別 US 判断であり本節の範囲外。

## 3. Target-resolution semantics

> **Redmine #13305 で route authority を収束 (design record #13305 j#73008)。** 本節の
> lane-less `(workspace_id, role)` projection (`resolve_herdr_target`) は **実 `handoff send`
> path の route authority ではなくなった**。実 send path は §3.1 の backend-neutral route
> authority (lane-in-match `(workspace_id, lane_id, role, pane_name)`) を経由する。
> `resolve_herdr_target` は **legacy compatibility adapter** として残し (translator fallback
> `handoff_transport_wiring._herdr_native_assigned_name` 用)、下記手順はその legacy adapter の
> 仕様として保持する。

入力: `receiver` (`claude` / `codex` / `coordinator`)、検証済み `SenderIdentity`、live
`agent list` rows、coordinator の provider binding。出力: 単一 target agent の assigned name +
transient locator (`resolve_herdr_target`, **legacy adapter**)。

手順:

1. `receiver` → target role:
   - `coordinator` → `RoleProviderBinding.provider_for("coordinator")` (既定 `codex`, #13174 /
     #12673)。binding が coordinator を bind しない → fail (`coordinator_binding_unresolved`)。
   - `claude` / `codex` → その値を target role とする。
   - それ以外 → fail (`unknown_receiver`)。
2. rows を走査し `decode_assigned_name(row.name)` で decode。decode 不能な row は
   mozyo 管理外 agent とみなし skip する。
3. `decoded.workspace_id == sender.workspace_id` かつ `decoded.role == target_role` の row を
   candidate とする (**workspace + role scope**)。lane は本 US の target 一致キーに含めない —
   純 herdr の単一 session (workspace あたり role ごとに 1 agent) を対象とし、multi-lane cross
   routing は後続 US。
4. fail-closed case (full case list):
   - candidate 0 件 → `no_match` (role 不在 = role mismatch、別 workspace の row 除外 =
     workspace mismatch はいずれも本 reason に畳む。detail で区別する)。
   - candidate が複数の distinct name、または同一 name の重複 row (duplicate assigned name) →
     `multiple_matches` (herdr name uniqueness 違反を推測せず fail)。
   - candidate 1 件だが row に usable locator (`pane_id`/`pane`/`location`) が無い →
     `missing_locator` (空 target への送信を拒否)。
5. 成功時は matched row の assigned name + locator + decoded identity を返す。呼び出し側は
   その assigned name を `rebind_by_name` で fresh snapshot に再照合してから port に渡す。

## 3.1 実 send path route authority (Redmine #13305 収束)

実 `handoff send` path (`orchestrate_handoff` → `application/herdr_send_entry.resolve_herdr_send_target`)
は §3 の lane-less match ではなく、**単一の backend-neutral route authority** を経由する
(`application/herdr_route_authority.resolve_herdr_route_target`)。tmux path が使う route-identity
ledger と同じ match key `(workspace_id, lane_id, role, pane_name)` に収束させ、route authority を
両 backend で単一化する。

- **route authority = lane-in-match。** canonical assigned name (`encode_assigned_name`) が
  `pane_name` を担い、live `agent list` row は #13247 decode で ledger row 形へ正規化してから
  `backend_neutral_resolver.resolve_route_neutral(..., backend=herdr)` で再照合する。herdr locator /
  pane_id は cache/evidence のみで authority に昇格しない。
- **lane は決定的に導出、全 lane scan しない。** lane 未指定 send は先に単一 lane を導出してから
  その slot を照合する。precedence (最優先から): **explicit lane > sender same-lane (peer
  `claude`/`codex` receiver は sender の lane) > coordinator default (`coordinator` は workspace
  default lane) > legacy default (sender lane 不明/`default`)** (`derive_target_lane`)。導出 lane の
  slot が live でなければ `target_unavailable` / `target_ambiguous` / `route_locator_missing` で
  fail-closed し、`(workspace_id, role)` の全 lane scan に fallback しない。explicit lane の CLI
  field は `handoff send --target-lane <lane_label>` (Redmine #13377): shared project workspace で
  coordinator→lane-gateway dispatch が同一 workspace 内の lane slot を明示する。`--target-repo
  <lane worktree>` は repo/cwd gate のままで、workspace selector にしない (j#73613)。
- **coordinator pseudo-target の send-entry translation (Redmine #13476, design consultation j#74599
  Option A)。** gateway→parent coordinator callback の backend-neutral documented form
  `--to codex --target coordinator` を維持する。`--target coordinator` は tmux pane resolver の
  `COORDINATOR_LABEL` と同一の semantic route identity (pane/location ではない) であり、herdr locator
  でもない。よって send entry (`resolve_herdr_send_target`) が `args.target == coordinator` を検知したら
  route 解決の receiver を `coordinator` へ translate し、route authority に `coordinator` receiver →
  coordinator provider (role) + workspace default lane (`derive_target_lane` tier 3) を解決させる —
  sublane sender の same-lane (tier 2) ではなく親 coordinator へ届く (Review #13476 j#74511 Finding 1 の
  same-lane misroute の修正)。explicit `--target-lane` は tier 1 として依然優先し、意図的 override を
  無視しない。`--to` public choices は `claude`/`codex` のまま (internal semantic translation)。outward
  receiver (`to=codex` marker / `binds_receiver` gate) は不変で、coordinator は codex なので role binding
  は一致する。default-lane coordinator が live でなければ fail-closed (same-lane に silent fallback しない)。
- **same-lane worker dispatch も explicit lane を pin する (Redmine #13485)。** herdr の
  `sublane dispatch-worker` (gateway→worker leg) は `read_lane` inventory decode が確定した
  worker locator を `--target` に載せるが、route authority はその locator を捨てて lane を導出
  し直す。よって worker dispatch は coordinator→gateway leg と同様に `--target-lane <lane_label>`
  を pin し、stable `(workspace_id, lane_label, claude)` identity へ解決させる。pin しないと rail は
  **sender の lane** を導出する (`derive_target_lane` tier-2 same-lane / tier-4 legacy-default) —
  coordinator / cross-lane stall-drive は workspace default lane で attested され target sublane と
  乖離するため、別 (default-lane) の `claude` slot を解決し、send は exit 0 で delivery-ACK しても
  実 lane worker は idle のまま turn 開始しない (#13483 j#74570 の ACK↔turn-start 乖離)。lane を pin
  すれば ACK は intended worker への submit 完結を測る (turn-start observation は別 telemetry で不変)。
  cross-lane drive は `--allow-direct-worker` (gateway-route 例外 #12918) がある時のみ admit される
  (#13483 j#74578 passing route)。tmux path は explicit `%pane` target で lane 導出 rail に乗らない
  ため `--target-lane` を付けず byte 不変。
- **fail-closed 語彙 = #13302 ledger 語彙。** 新 reason token は増やさない (必要時は再 consultation)。
- **gateway-route enforcement gate との関係。** cross-lane worker 送信 (governed
  `implementation_request` `--to claude` を別 lane worker へ) は、lane-in-match により
  **target 解決の時点で `target_unavailable` に落ち**、gateway-route gate に到達する前に
  fail-closed する (同一 invariant を上流で enforce)。gate 本体は tmux path 用に byte 不変で残す。
- **tmux path は byte 不変。** tmux は従来どおり `pane_info` で解決する。`resolve_route_neutral(tmux)`
  が `pane_info` の target と一致することは characterization test で pin する
  (`tests/unit/.../test_herdr_route_authority.py::TmuxByteInvarianceCharacterizationTest`)。

## 4. Discovery-port boundary (core vs provider)

- **core が所有** (`domain/herdr_target_resolution.py`): sender identity contract、target role
  語彙、fail-closed reason 語彙、`resolve_herdr_target` の pure projection、discovery Port
  Protocol (`HerdrAgentDiscoveryPort`)。ルーティング権限 (どの label が誰に解決されるか) は core。
- **provider が所有** (`infrastructure/herdr_discovery.py`): `herdr agent list` の subprocess
  実行と row 抽出 (`_extract_list_rows` を #13246 と共有)。binary は trusted env
  (`MOZYO_HERDR_BINARY`) からのみ解決し、repo config は backend 選択のみ (#13245 と同一姿勢)。
- `TerminalTransportPort` は **拡張しない**。discovery は send-safety port と別の listing/preflight
  Protocol として同 bounded context に追加する (auditor 回答 j#72519)。
- herdr は `BUILTIN_PROVIDER_REGISTRY` に TERMINAL_RUNTIME provider として登録しない
  (#13245 default-selection ambiguity 回避)。`terminal_transport.backend: herdr` flag が唯一の
  selector。

## 5. session-start one-command (write side)

`mozyo-bridge herdr session-start` (`application/herdr_session_start.py`)。明示 opt-in。backend
flag には結合しない (別々に選べる) が、純 herdr 運用では両者を併用する。

flow:

1. herdr binary を trusted env から解決 (未設定 / 未解決 → fail-closed)。
2. workspace segment を単一 resolver `herdr_workspace_segment(repo_root)` で得る (§1 の workspace
   field と同定義)。**standalone / main checkout** は execute path では `register_workspace` /
   `read_anchor` を再利用し registry workspace_id を得る (空なら fail-closed、従来どおり)。
   **`--dry-run` は query / command 分離 (Redmine #13595): `register_workspace` を呼ばず
   `_resolve_workspace_id_readonly` で read-only 解決する** (anchor が id を pin、無ければ registry
   row。registry / anchor / `last_seen` を一切 write しない)。durable identity が未確定 (anchor も
   registry row も無い) / 両 anchor 名併存 (write path と同じ曖昧性) の場合は fake identity を作らず
   actionable に fail-closed し、silent registration しない (旧実装は dry-run 分岐前に
   `register_workspace` を呼び registry + anchor を mutate していた)。**linked git worktree lane
   (Redmine #13377 / j#73613)** は shared resolver `herdr_workspace_segment` 経由で main checkout の
   **registry row → anchor** precedence で project workspace_id を継承する (#13152 canonical
   inheritance と同値、#13595 R1-F1。registry-only main も継承し、mint==resolve を維持。main が
   registry row も anchor も持たなければ fail-closed)。dry-run / execute とも同 resolver を使うため
   preview は execute と一致する。lane segment は明示 `--lane` か、`sublane create` が書いた lane
   metadata record の `lane_id` から復元し、どちらも無ければ fail-closed する (lane worktree から
   project workspace の default slot — coordinator pair — を誤 mint しない)。launch 先 herdr
   workspace は lane-aware join (`_launch_target_for_lane`, Redmine #13380) で決める: (1) 自
   lane の live slots + 同 run adopted slots が pin する workspace (heal で pair を分裂させない)、
   (2) 非 default lane は、他 lane slots が占める workspace から live default-lane slots
   (coordinator pair) の workspace を除外した残り = **sublane host workspace**、(3) どちらも
   無ければ workspace create する (lane slot は operator 可読 `--label` 付き、cosmetic のみ —
   join key は常に live mzb1 inventory)。default lane は自 pin のみ join し host へは決して
   join しない。各段で pin が複数 workspace に split したら fail-closed。lane ゼロの host は
   herdr が最終 pane close で自動 close する (実測、#13380) ため残骸 husk は構造的に生じず、
   次の lane が on demand で再 mint する (per-lane workspace は作らない)。同 resolver を
   send / retire / projection の resolve 側でも使い、mint と resolve を一致させる。
   **さらに非 default lane は host workspace 内の tab を lane-aware join
   (`_tab_target_for_lane`, Redmine #13411) で決める: (1) 自 lane の live slots が pin する tab
   (heal / 混在 adopt+launch は生存 slot の `tab_id` を読んで同一 tab へ復帰、pair を分裂させ
   ない)、(2) 自 slot が無い fresh lane は `herdr tab create --workspace <host> --label <lane key>`
   で tab を mint する (label は cosmetic、join key は `tab_id`)。自 slot が loose pane (pre-#13411、
   tab_id 無し) の heal は loose のまま launch する (pair を新 tab へ分裂させない。full relaunch で
   tab へ移行)。自 slot が複数 tab に split したら fail-closed。default lane は tab を使わない
   (byte-invariant)。launch は `agent start --workspace <host> --tab <tab_id>` で行い、tab 内 2
   slot 目 (fresh pair の第 2、または heal で生存 slot の隣) は `--split <dir>` を付ける。方向と
   provider 順序は `lane_placement` config で lane class 別に宣言できる (Redmine #13646、下記
   §lane_placement)。未設定時は従来どおり sublane が `--split right`、default lane は `--split`
   を出さず herdr server 既定へ委任する (byte-invariant)。tab root pane は
   #13330 の workspace base pane と同型で全 launch 成功後に reclaim し、tab 内最終 pane close で
   herdr が tab を自動消滅させる (workspace 自動消滅と対称)。**
3. mint durable name: `encode_assigned_name(workspace_segment, role, lane)` で mzb1 名を作る。
4. 要求 agent (`claude` / `codex`) を herdr 管理 agent として **durable 名を start 時に付与**して
   launch する (下記 launch contract)。self-identity (`MOZYO_WORKSPACE_ID` /
   `MOZYO_AGENT_ROLE` / `MOZYO_LANE_ID`, §2) は `--env KEY=VALUE` で spawn 先へ渡す。
   **startup self-attestation wrap (Redmine #13637)**: provider は直接ではなく
   `mozyo-bridge herdr agent-attest --assigned-name <NAME> --workspace-id <WS> --role <PROVIDER>
   --lane <LANE> -- <provider argv...>` を通して起動する。この wrapper は agent 自身の process として
   走り、自 env を期待 identity と照合して §2 の startup self-attestation record を書いてから provider を
   `exec` する (self-check before exec)。mozyo-bridge launcher が trusted env (絶対 PATH / 明示
   override) で解決できない場合は wrap せず直接 provider を起動する byte-invariant fallback を採り
   (dead pane を作らない)、record 不在は adopt / doctor 側で fail-closed に縮退する。
   **launcher command-capability preflight (Redmine #13748)**: launcher が解決されて wrap する場合、
   実 launch (workspace / tab / agent いずれの side effect) より前に、その launcher の CLI が
   `herdr agent-attest` wrapper subcommand を実際に実行できるかを actuation-free に probe
   (`herdr agent-attest --help`) する。単なる実行可能ファイル確認では compatible とみなさない。
   **exit 0 単独でも不十分** (review R1): 引数を無視して 0 終了するだけの non-launcher
   (`/usr/bin/true` 等) も通過してしまい、実 launch は同 launcher を wrapper argv[0] にするため
   provider を exec せず即死する。よって capable 判定は **exit 0 かつ probe 出力に marker
   `--assigned-name`** (wrapper が実際に渡す flag、real の `agent-attest --help` に出現) を含むことを
   要求する。installed launcher が未リリース source に遅れて subcommand を欠く場合
   (installed 0.10.0 は argparse exit 2、source tree は成功) 各 wrapped pane は provider 起動前に
   即死し、`sublane create` は一度返した live locator を失う。probe が capability を確認できない場合は
   workspace/tab/agent を作らず fail-closed し、error に launcher path・必要 command・復旧 action
   (release/install または明示 `MOZYO_BRIDGE_LAUNCHER` override) を示す (credential / 個人 path は
   durable log へ残さない)。unwrapped fallback (`attest_launcher == ""`) と adopt-only / dry-run は
   wrapper を走らせないため probe せず byte-invariant のままとする。
5. idempotency: 対象 slot の mzb1 名を既に持つ live agent があれば **adopt** (再 launch しない)。
   ただし adopt は live name-match だけでは足りず、その live locator に **generation-bind した
   `present` startup self-attestation record** (§2 / #13637) が必要である。record 不在 (legacy /
   pre-feature slot) / stale (locator 世代不一致) / `missing` / `conflict` は blind-adopt せず read-only
   の **`unattested`** として exact reason + owner 承認 close+relaunch next action で surface する
   (自動 close/relaunch はしない)。slot に別 locator の同名 agent が複数ある (duplicate) → fail-closed。
6. slot-uniqueness (要求側、#13261 j#72532): 要求された `(provider, lane)` slot が重複する場合は
   **いかなる side effect (binary 解決 / registration / inventory snapshot / launch) より前に**
   fail-closed で拒否する (silent 正規化しない)。同一 slot を二重に prepare すると同じ mzb1 名を二度
   mint し read side が `multiple_matches` で落ちるため。CLI の `--agent` は repeatable のままでよい
   (重複入力を die で弾く)。

### launch contract (herdr 0.7.1 live-measured, coordinator pre-smoke)

staged assumption は解消済み。実 herdr 0.7.1 で計測した確定仕様:

```
herdr agent start <NAME> [--cwd PATH] [--env KEY=VALUE]... [--no-focus] -- <argv...>
```

- `<NAME>` は **必須 positional** で start 時に直接適用される (probe: `result.agent.name == <NAME>`)。
  mozyo は mzb1 durable 名をここで付与し、**別途 `agent rename` を発行しない**。
- self-identity var は client process env では **spawn 先に届かない** (server-spawned agent は
  client env を継承しない、実測)。よって `--env MOZYO_WORKSPACE_ID=...` / `--env MOZYO_AGENT_ROLE=...`
  / `--env MOZYO_LANE_ID=...` で渡す。
- `--no-focus` で operator focus を奪わない。
- 出力は stdout 上の単一 JSON object。rebind/read 用 transient locator は
  `result.type == "agent_started"` envelope 下の `result.agent.pane_id`
  (`_parse_started_locator`、type 不一致 / pane_id 欠落は fail-closed)。
- **managed Claude の permission-mode parity (#13360 / #13397)**: `-- <argv...>` の claude 起動列には
  #11925 policy (env `MOZYO_CLAUDE_PERMISSION_MODE` override > launch-context default > なし) で
  解決した `--permission-mode <mode>` を付与する。sublane lane 作成 chokepoint は default `auto` を
  渡し (tmux `cockpit append` parity、lane worker の prompt stall 防止)、bare `mozyo` の coordinator
  pair launch (`herdr_launch_command`、default no-lane session の claude + codex) も同じ default `auto`
  を渡す (#13397 finding 3 — 外部 project で coordinator Claude が manual mode 起動し headless 運用不能
  だった非対称を lane worker parity で解消。env override は常に有効)。direct `herdr session-start` CLI
  (`cmd_herdr_session_start`) も同じ default `auto` を渡す (#13452 / #13453 — runbook の relaunch command
  単体で live argv が `sublane readiness` の `auto` projection と一致する parity。それ以前は CLI だけが
  `None` を渡し flagless=manual だった)。default `None` を渡す caller は歴史的 flagless bare `claude`
  起動のまま。codex には付与しない。invalid mode は launch を fail-closed。

自動テストは injected runner で argv + JSON parse を検証する (live binary は不使用)。end-to-end
live smoke は coordinator の post-review step。

### 空 base pane の回収 (cold start、#13330)

herdr workspace は生成時に必ず `root_pane` (agent 無しの空 base shell) を 1 個持つ (実測:
`workspace create` 応答 = `result.type == "workspace_created"` に `result.workspace.workspace_id`
+ `result.root_pane.pane_id`、`pane_count: 1`)。cold start で初回 `agent start` を `--workspace`
無しで呼ぶと herdr が workspace を暗黙生成し、この root pane が使われない残骸として agent pane の横に
残る (dogfood 発見 #12)。回収は次の決定的手順で行う (auditor ruling #13330 j#73225、対処 (a) 採用):

1. 全 slot を launch 前に分類する (adopt / launch / dry-run plan)。
2. launch する slot があり、かつ adopted agent が既存 workspace を pin していない (pure cold start) 場合は
   **明示的に** `herdr workspace create --cwd <repo> --no-focus` を呼び、応答の `workspace_id` と
   `root_pane.pane_id` を保持する。応答が parse 不能なら fail-closed (推測で pane を閉じない)。
3. 各 launch slot を `agent start --workspace <workspace_id>` で起動する (herdr が second workspace を
   暗黙生成しない)。
4. **全 launch 成功後に限り** `herdr pane close <root_pane_id>` で、この run が生成した root pane
   **のみ**を閉じる。

fail-closed / safety 不変条件:

- 閉じる対象は **この run が `workspace create` で得た `root_pane.pane_id` 一点のみ**。scan で「空
  shell らしき pane」を探して閉じることは禁止 (user 自身の shell を誤 close しない構造的保証)。
- **launched locator は target workspace 内であることを fail-closed 検証する** (#13330 review j#73231)。
  `agent start --workspace <id>` の返す `result.agent.pane_id` の workspace prefix が要求 workspace と
  一致しない場合 (herdr が flag を無視 / 仕様差分で別 workspace に auto-create した場合) は
  `HerdrSessionStartError` で raise する。検証は reclaim step より前で発火するため、mislocated launch
  時は created root pane を close せず、別 workspace 側の残存 base pane を見逃さない。
- launch 失敗は reclaim より前に raise する (created workspace / root pane は残骸として残し、実装失敗
  として扱う。blind close しない)。
- `pane close` 失敗は **non-fatal** (agent slot は既に live で、空 base pane は cosmetic 残骸)。
  `SessionStartResult.base_pane_detail` に記録し、session-start 全体を hard-fail しない。
- all-adopt / 既存 workspace への launch は base pane を新規生成しないため byte-invariant。
- workspace_registry schema は無変更 (§2 invariant 維持)。herdr terminal workspace id は
  `SessionStartResult.herdr_workspace_id` (created / adopted prefix) として観測用に運ぶだけで、mozyo
  registry には持ち込まない。mixed adopt+launch では adopted locator の `wN` prefix から launch target を
  導出し、複数 workspace prefix が混在する場合は fail-closed。

live smoke (cold start bare `mozyo` 後に root pane が残らないこと、adopt 経路が byte-invariant で
あること) は coordinator の post-review 実機 acceptance で確認する。

## 5.1 lane_placement — pair 配置の設定駆動化 (Redmine #13646)

herdr pane pair の **split 方向**と**役割順序** (どちらの provider が先 = 左 / 上に置かれるか) を
lane class 別に宣言する closed config block。`.mozyo-bridge/config.yaml`:

```yaml
lane_placement:
  default:                    # coordinator / auditor pair (bare `mozyo`)
    split: down               # right | down
    order: [codex, claude]    # exact permutation
  sublane:                    # lane gateway / worker
    split: right
    order: [codex, claude]
```

### Schema (fail-closed)

- lane class key: `default` | `sublane` (`agent_launch` と同じ lane-class 軸。ただし別関心 =
  pane geometry であり、launch-argv token 軸とは resolve を分離し混同しない)。
- `split`: `right` | `down`。herdr 0.7.1 `agent start --split right|down` の語彙 (実 `--help` 照合)。
- `order`: `[codex, claude]` / `[claude, codex]` の **exact permutation**。欠落 / 重複 / 未知 provider /
  非 list は fail-closed (部分順序が silent に provider を落とさない)。
- lane class object 自体・`split`・`order` はそれぞれ **個別に optional**。欠落した field だけが
  legacy 規律を継承する (空 `{}` は no-op)。
- unknown class / unknown key / unknown value / unsupported version は fail-closed。

### Compatibility (byte-invariance)

- **未設定は現行 launch argv と byte 一致**: `sublane` は従来どおり 1st slot が tab を占有し 2nd slot が
  `--split right`、`default` は `--tab` も `--split` も出さず herdr server 既定へ委任する。
- 設定した field だけが差分を生む。`default` を設定しても `sublane` の launch は不変 (逆も同様)。

### Launch semantics

- **fresh pair**: `prepare_session` が `order` で requested providers を並べ替える。1st slot が container
  (default = project workspace / sublane = lane tab) を占有し (`--split` 無し)、2nd slot が
  `--split <dir>` で隣に置かれる。`--split` は `--tab` と独立に出せる (herdr 0.7.1 は両者を独立 optional
  flag として受理) ため、tab を持たない default pair も縦分割できる。
- **active-target と first-launch `--focus` (Redmine #13646 R1-F1、実機実測)**: herdr の split は
  **container の active pane** を割る。`agent start` に pane-target flag は無い。全 launch を
  `--no-focus` にすると container の空 root pane が active のままなので、2nd slot の `--split <dir>` は
  **1st agent ではなく root pane** を割り、その root を reclaim した時点で nested split が畳まれ、1st agent
  の暗黙 split が作った外側の既定 `right` だけが残る = **設定方向が無言で効かない**。
  → **fresh container で explicit placement を実現する時だけ、1st launch を `--focus`** にして split target
  を 1st agent へ固定し、2nd 以降は `--split <dir> --no-focus` とする。root pane reclaim は従来どおり
  **全 launch 成功後** (partial-launch safety を壊さない)。
  発火条件は狭く限定する: container occupancy = 0 かつ launch 対象 2 件以上かつ `split`/`order` が
  explicit。**unset / single-provider / heal / mixed adopt では発火しない** (byte/layout invariant)。
  なお `--split right` literal (#13411) も同じ理由で本来効いておらず、観測される `right` は herdr 既定
  split の偶然の一致だった。unset lane class はその挙動をそのまま維持する。
- **single-provider request**: `order` は **未要求の peer を暗黙 launch しない**。heal は欠けた provider
  だけを launch する。
- **heal**: 生存 sibling の隣へ configured `--split <dir>` で launch する。既存 pane は swap / move /
  bounce / focus しない (container の唯一の pane = 生存 sibling が既に split target なので `--focus` 不要)。
- **order best-effort**: herdr `agent start` に pane-target flag は存在しない (実 `--help` 照合) ため、
  役割順序は **launch 順としてのみ**実現できる。configured primary (`order[0]`) が後から復旧し、生存
  sibling の隣へ split するしかない場合は物理順序を満たせないので、その slot の `detail` に
  `order_deferred_until_full_relaunch` を出す (silent に「order 適用済み」と主張しない)。full relaunch で
  configured order が物理的に実現する。

### Boundary

- `lane_placement` は **future launch policy** であり、live layout / liveness / route authority ではない。
  config を読むだけで既存 live pair を移動しない (herdr は same-tab re-split を拒否する。live 再配置は
  operator の CLI 操作のまま)。
- config key は `pane_placement` では **なく** `lane_placement`。repo-local schema boundary
  (`_FORBIDDEN_KEY_PARTS`) は `pane` を含む key を allowed-key 判定より前に拒否するため、live pane
  addressing に見えない名前へ寄せている (boundary screen は緩めない)。

### 拡張点 (v1 非対象)

owner の「親子孫 3 層それぞれで変えたい」要望のうち、**layer 別 (親 / 子 / 孫 lane role 別) の key 分けは
v1 に含めない**: launch 経路の語彙は現状 lane_class (`default` / `sublane`) の 2 値であり、layer 別 keying
には lane-role 語彙の launch 時解決が別途必要。schema は lane class key を追加するだけで拡張でき
(closed set に新 class を足す)、既存 class の意味論は変わらないため、この拡張点は塞がっていない。

## 5.1.1 coordinator placement mode — operator-scoped 配置 (Redmine #14139)

coordinator pair (default lane) を **どの herdr workspace に置くか**を operator ごとに切り替える
closed knob。§5.1 `lane_placement` (pair 内部の split 方向 / 役割順序、repo-committed) とは**別関心・別
source**であり、`_launch_target_for_lane` (#13380) / `_tab_target_for_lane` (#13411) の sublane 配置軸は
一切変えない。

### Scope は operator-scoped (home-level、非 commit)

設定は mozyo-bridge **home** root の `coordinator-placement.yaml` に置く (`mozyo_bridge_home()` = `MOZYO_BRIDGE_HOME`
または `~/.mozyo_bridge`)。repo-committed config に置かない理由は portable 値 vs operator-private 境界: 同じ N
repo を扱う 2 人の operator が「全 project の coordinator を 1 window で俯瞰したい」「小型モニタで project 別に
切替えたい」と正当に対立し、committed 値は N repo 間で衝突し、operator の私的選好を上書きしてしまう。repo に
残るのは pair 内部配置 (`lane_placement`, #13646/#13647) までとする。file は本 mode 専用の小 file とし、repo-local
schema とも将来の home-config schema (#14148) とも衝突させない。

```yaml
# ~/.mozyo_bridge/coordinator-placement.yaml
mode: shared_space          # per_project_space | shared_space
```

### Closed vocabulary (unknown fail-closed)

- `per_project_space` (**既定**、file 不在時): coordinator pair は各 project の project workspace に置く
  (#13380 の従来動作)。opt-in しない operator は pre-#14139 と byte 一致で起動する。
- `shared_space`: 全 project の coordinator pair を **1 つの stable shared coordinators workspace** に置き、
  project ごとに column とする (tmux 時代の俯瞰運用の復元)。
- それ以外の `mode` 文字列 / unknown key / 非 mapping / unsupported version は
  `CoordinatorPlacementError` で fail-closed (未知 shape が per_project_space に化けない)。

### shared_space の workspace identity / label authority / 冪等 adopt

shared space の identity は **backend が read できる stable workspace label `coordinators`** が authority で
ある (R1 review j#83383 F1 / Design Answer j#83385 Decision 1)。**locator prefix だけで shared space を
認定してはならない**: per-project coordinator workspace と shared workspace は inventory 上区別できず、prefix
guess は mode 切替時に per-project window を誤 adopt する。label は create 時に付与し、adopt/join は
action-time に `herdr workspace list` の **exact label (verbatim、trim / case-fold しない)** を再読して判断する
(R4 review j#83473 F1: `"  coordinators  "` や `"Coordinators"` は別 label で adopt しない)。**per-project
workspace を暗黙に shared へ昇格・relabel しない。**

own-pin (自 project の live/adopted default-lane slot) は自 identity が pin するので **label read を要しない**。
その解決は label read の **前** に行い、own pin が存在すれば `workspace list` を発行せず join する (R4 review
j#83473 F2: own-pin heal は `workspace list` command の成否に依存しない)。own pin が無いときだけ label を読む。

`shared_space` の default-lane target 解決 (`herdr_lane_topology._shared_coordinator_target(rows, workspace_id,
adopted_locators, workspace_labels, shared_label)`):

1. **自 project の live/adopted default-lane slot** が pin する (heal は coordinator pair を workspace 跨ぎで
   分割しない)。自 identity が pin するので label read は不要。
2. 自 pin が無ければ label authority で判断する (`workspace_labels` = `{herdr_workspace_id: label}`、
   `herdr workspace list` で action-time 取得):
   - **`workspace_labels` が読めない (None)** → typed fail-closed (推測しない)。
   - `shared_label` を持つ herdr workspace (= labelled candidate)。**live default-lane slot の有無を問わない**
     (R5 review j#83516 F1): create 後 agent-start が失敗した **partial-failure husk** や、single-flight fence
     下で先行 process が create したがまだ launch していない space も shared space であり adopt 対象とする:
     - **ちょうど 1 つ** → その space を adopt する。#13380 の sublane host 解決と違いここは意図的に mozyo
       `workspace` identity 境界を跨ぐ (各 coordinator は自 project identity `mzb1_<project-ws>_<role>_default`
       を保つ) が、境界跨ぎは **label 一致に gate される**。これが 2 番目以降の project の launch を
       「先行 project が作った space の冪等 adopt」にする。
     - **複数** → ambiguous shared space として fail-closed。
3. labelled candidate が無い場合:
   - **他 project の coordinator pair が live だが shared label を持たない (per-project workspace)** → fail-closed
     (mode-transition guard: per-project window を暗黙昇格しない)。
   - coordinator pair が 1 つも live でない (clean slate) → `""` → caller が stable label `coordinators` で create。

### create の single-flight fence (concurrent 収束)

husk-adoption だけでは **clean-slate 同時起動** race は閉じない (双方が「labelled candidate 無し」を読んで双方
create する)。launch admission は attestation store lock を **shared** で持つため create を直列化しない。そこで
shared default-lane の **list→resolve→create を home-scoped exclusive advisory lock**
(`core/state/coordinator_placement_fence.coordinator_shared_create_lock`、`attestation_store_lock` と同じ
`fcntl.flock` protocol、別 lock file で相互非干渉) の下で実行する。lock 取得後に label を再読して resolve
(double-checked) し、無いときだけ create する。よって同時起動でも create するのは 1 process だけで、他は待機後
再 resolve で husk-adoption/adopt に収束し、**shared workspace は 1 個**になる。**own-pin heal は lock を取らない**
(create しない = R5 F2 契約維持)。lock は home 下の 0600 advisory artifact で state を持たず、operator config
write ではない (`flock` のみ)。lock protocol 不能 / home permission / acquire error は list/create の**前**に起きるので
**zero herdr actuation** で session-start の typed error (`HerdrSessionStartError`) へ変換して fail-closed する
(R6 review j#83569 F2、public CLI が raw traceback にならない)。concurrent 収束は `threading.Barrier` +
共有 fake backend + `fcntl.flock` の別-fd 競合で **create count 1** を deterministic に regression 固定する
(live Herdr smoke 不要)。

sublane slot は coordinators space を pin しない (default-lane slot のみ consult する)。自 pin が複数 herdr
workspace に跨る場合は identity conflict として fail-closed (#13330 posture)。この label read / fence は shared_space の
default-lane path でのみ発火し、`per_project_space` と全 sublane launch は `workspace list` も lock も発行せず
byte-invariant を保つ。

### project 列順 — deterministic append order (not arbitrary live reorder)

Herdr の public launch API は既存 workspace 内への任意 insert / reorder target を持たない (`agent start` に
pane-target flag 無し)。したがって**独立 launch を跨ぐ厳密な左右順は保証しない** (R1 review j#83383 F2 /
Design Answer j#83385 Decision 2 / premise 訂正 j#83433)。

現行 architecture では coordinator launch (`herdr_launch_command.prepare` の bare `mozyo` / `herdr_session_start_cli`)
は **単一 project の coordinator pair を 1 回だけ** 起動する。複数 project を一括生成する batch seam は存在しない。
したがって **current-scope の acceptance** は次の realizable invariant である (j#83433):

- 単独 project の coordinator launch は backend の既存列 **末尾へ append** する。
- 既存 column の順序を変更せず、**live reorder / relayout を行わない** (既存 pane を move / swap / close しない)。
- resolver の adopt / create / fail-closed 判断は **inventory row の iteration 順に依存しない** (集合演算 + sorted)。
- **duplicate workspace identity** は label 一致 / 不一致に関わらず fail-closed し、**順序を反転しても同一 verdict** となる
  (`_parse_workspace_list` は重複 `workspace_id` を検出したら `None` へ倒す)。

**将来 invariant (現行 R3 は未実装)**: 複数 project を一括生成する実在 batch seam を追加する場合、その seam は
stable project key 順に append する。これは今回、未使用 helper や架空 batch path を追加する根拠にはしない。

厳密左右順を望む operator の live 再配置は live-relayout runbook (#13648) の領分であり、本 mode は
**deterministic append order であって arbitrary live reorder ではない**。

### Launch-time only (適用条件)

mode は **launch / adopt 時のみ**読む。設定を変えても既存 live pair は自動で動かない (herdr は same-tab
re-split を拒否する; live 再配置は live-relayout runbook のみ, #13648)。適用は **次回の fresh launch / adopt**
から。config 読取りは composition root (`herdr_launch_command` の bare `mozyo` coordinator launch /
`herdr_session_start_cli`) で行い、pure な `prepare_session` へ解決済み mode 文字列を渡す (ambient IO を pure core に
持ち込まない)。壊れた operator file は composition root で actionable に fail-closed する。

### Compatibility

- 未設定 = `per_project_space` = pre-#14139 と byte 一致 (project workspace は無 label で create)。
- `shared_space` が分岐させるのは **default lane のみ**。同 mode 下でも sublane launch は #13380 host label
  (`<project>_sublanes`) を保ち、`coordinators` にはならない。

## 5.2 mutating-heal runtime fence + `pair_split` projection (Redmine #13705)

§5 の同一 tab pair placement / heal contract は、**それを実装した runtime が heal を
実行する**ことを前提にする。実測 incident (#13705): #13411 contract を持つ source
(`c4a999e`) で作った lane を、同 contract を欠く古い installed runtime (pipx 0.10.0) で
heal したため、replacement gateway が surviving worker と別 tab に着地し、`sublane list`
は依然 `active` を返した。直接原因は runtime/source skew だが、製品欠陥は mutating
actuation が **pane 生成前に実行 runtime の placement-contract capability / build
provenance を照合しない**ことにある。

- **front-door fingerprint gate (R1-F1、mutation 前 zero-write)**: mutating
  `sublane start/heal --execute` の official 入口 (`SublaneActuateUseCase`、全 side-effect
  前) が **action-time runtime fingerprint** を照合する。`doctor runtime` の
  active-vs-repo-local-source drift 判定 (`evaluate_fingerprint`) に **placement probe**
  `same_tab_pair_placement` (source marker `def _tab_target_for_lane` + active `hasattr`
  probe) を加え、active runtime が source の同 placement behavior を欠く drift
  (`probe_mismatch`) を `evaluate_mutation_placement_gate` が検出したら
  worktree/tab/agent write 0 で fail-closed する。これは capability 自己申告でなく **実
  active-vs-source probe** による skew 検出であり、issue Scope の「runtime/build
  fingerprint」選択肢を満たす。`preflight_runtime_placement_gate` は optional/herdr-only
  port method で、fingerprint reader は test に inject 可能。
- **residual の設計上明示 (authority boundary 不在)**: fence code を一切持たない古い client
  (事故の installed 0.10.0) は、本 runtime が出す code では止められない — herdr backend は
  mozyo client を拒否する authority を持たず、lane lock/lease を古い client は読まない。
  従って本 fence は **forward gate** である: 修正 runtime が installed になった後、stale
  install が newer source に対して mutating すれば front-door gate が zero-write で拒否する
  (skew の現実的再発形)。事故そのものの residual は **#13524 reinstall fingerprint gate**
  (source/installed fingerprint 一致を確認してから dogfood 再開、Close condition #5) で閉じる。
  per-lane に required contract を stamp する案は採らない: front-door fingerprint gate が同
  skew class を token 比較より一般に (any placement-behavior drift) 検出するため冗長。
- **heal capability fence + pair invariant preflight/postcondition**: heal 個別 path も
  defense-in-depth を保つ。`heal_lane_column` は pane 生成前に純 fence
  `sublane_runtime_fence.evaluate_heal_runtime_fence` を評価 (`runtime_lacks_placement_contract`
  / `provenance_unknown` / 既分裂 live pair `pair_already_split` を write 0 で拒否)。
  **R1-F3 fail-closed**: preflight の inventory read 不能は side-effect 前 block、compatible
  heal 後の same-tab postcondition は inventory 不能・slot 欠落・非 co-located をいずれも
  fail-closed (unknown は success にしない)。legacy loose pair は既知 key `(wN, "")` の
  co-located として扱い unknown と混同しない。**R11 target-scoped postcondition (Redmine
  #13933 j#81429)**: pure contract は `sublane_runtime_fence.enforce_heal_postcondition`。
  default (`target_provider=None`) は上記 full-pair 契約を byte-identical に保つ。
  `heal_lane_column(target_provider=<provider>)` で単一 owed participant を launch する時
  (bound-pair convergence の 1 leg) は、target slot が live であることを要求し、sibling も
  live なら依然 co-located を要求 (live split は `pair_split` で fail-closed=same-tab placement
  を bypass しない) 一方、sibling **absent** は後続 leg が収束させる partial state として許容し、
  承認済み partial pair を恒久 `effect_failed` へ fence しない。fence は typed `SublaneHealError`
  (`launch_target_absent` / `pair_split` / `pair_incomplete`) を raise し、public outcome が
  `launch:<reason>` を surface する。
- **`pair_split` degraded projection + admission (R1-F2)**: projection
  (`project_herdr_sublanes` / `herdr_lane_view_for_worktree` / actuator `read_lane`) は各
  slot の `(herdr_workspace, tab_id)` を比較し、live pair が単一 container を共有しなければ
  `active` でなく domain state `pair_split` (`SUBLANE_STATE_PAIR_SPLIT`) を返す。さらに
  use case は `pair_split` lane を adopt/dispatch せず append/dispatch 0 で fail-closed する
  (`sublane_actuator_gates.pair_split_admission`、adopt/append/heal read-back の全 site)。
  既存 split lane の復旧は owner 判断の retire + recreate であり heal-over しない。placement
  key を渡さない caller (tmux projection) は byte-invariant に `active` を保つ (tmux の
  window 分裂は従来どおり `STALE_HINT_WINDOW_SPLIT` advisory)。

fence は何も修復せず、live process env を読まない (herdr は不可)。blocked からの復旧は
owner 判断 (runtime を `doctor runtime` で検証し source と一致する互換 runtime で heal /
recreate、split lane は retire + recreate) である。

> **acceptance (coordinator ratified、Redmine #13705 j#77203 = Close condition #1 の durable
> amendment)。** 上記 forward-gate + reinstall-gate による residual の扱いは、coordinator
> acceptance authority (owner delegation、production release 以外) により承認され、issue
> #13705 の Close condition #1 は次へ改訂された: *incompatible/provenance 不明 runtime による
> heal/start は、本 fence を carry する runtime の official mutating front door において
> workspace/tab/agent side effect 0 で fail-closed する。fence code を持たない旧世代 client は
> 本 issue scope では技術的に停止不能であり、その残余は #13524 の installed/source fingerprint
> 一致・local reinstall gate が green になるまで dogfood/release 候補へ進めないことで閉じる。*
> fence-less client 自体を backend/server authority で拒否する強保証が将来必要なら別 ticket
> (本 issue へ scope 膨張させない)。

## 6. Close-evidence contract (pure-herdr round trip)

close 判定には次の durable evidence を要求する:

- `TMUX` 未設定、または tmux server 不在 / 隔離 socket であること (session が純 herdr である証跡)。
- `mozyo-bridge herdr session-start` が claude / codex の mzb1 assigned name を mint した log
  (assigned name literal を含む)。
- handoff send が sender env + live inventory から target を解決した log (resolved assigned name /
  locator、fail-closed reason が出ていないこと)。
- 上記を記録した Redmine journal id。

live smoke (実 herdr binary + 実 agent) は coordinator の post-review 実機 acceptance で行う。
本 US の自動テストは全て fake runner で fail-closed 経路を網羅する。

## 7. tmux path freeze

backend=`tmux` の挙動は byte 不変: `tmux_client.py` / `pane_resolver.py` / `commands.py` の
send pipeline / `handoff_transport_wiring` の tmux 経路に behavior change を入れない。
herdr-native 解決は backend=`herdr` 選択時のみ有効。

### orchestrate-entry の backend-aware target 解決 (increment 2)

`orchestrate_handoff` は backend=`herdr` のとき、send target を tmux `pane_info` ではなく本 spec の
herdr-native 解決 (launch-time sender identity + live inventory) で解決し、downstream pipeline が
消費する pane record を synthesize する。synthesize record は **normal_window** projection
(role を `window_name` に載せ `@mozyo_agent_role` は付けない) とし、cockpit 前提の main-lane guard を
不活性にしつつ `binds_receiver` の strong role 判定は成立させる。tmux 専用 side step は backend=`herdr`
で明示 no-op にする (each に rationale コメント + テスト):

- `require_tmux()` — herdr では skip (tmux server 前提を課さない)。
- queue-enter の tmux-session binding gate / cross-session `--to claude` gate — herdr では no-op
  (tmux session 概念が無く、audit boundary は workspace-scoped inventory decode が担保)。
- same-lane duplicate pane snapshot — herdr では明示的に空 (tmux pane snapshot 依存)。identity uniqueness は
  assigned-name decode (duplicate name は fail-closed) が担保。
- select-pane 起点の target activation — #13253 shim の no-op 経由 (herdr は pane focus 不要)。
- **gateway-route enforcement gate の sender lane 解決 (increment 4)** — 従来は
  `current_pane_lane_unit()` → `pane_lines()` (tmux `list-panes -a`) を **backend 非依存で無条件に**
  呼んでおり、純 herdr で (a) no-tmux 契約違反、(b) sender identity unknown → gate が silent skip
  (cross-lane worker 送信が素通り) の 2 問題があった (実機 smoke で検出、j#72517 系)。increment 4 は
  backend=`herdr` のとき **env 由来 SenderIdentity** (`MOZYO_WORKSPACE_ID` / `MOZYO_LANE_ID`、target 解決で
  既に resolve 済みを thread) を `enforce_gateway_route(sender_lane_unit=...)` に渡し、gate は **env
  sender lane で enforce し tmux call を 0 にする**。tmux backend では `sender_lane_unit=None` で
  `current_pane_lane_unit()` 経路が byte 不変。純 herdr では sender lane は常に既知 (欠落は target 解決で既に
  fail-closed) なので gate は skip せず enforce する。

herdr send path の tmux-capable call site 全数監査 (increment 4): `require_tmux` / `pane_info` /
same-lane duplicate / queue-enter・cross-session session gate / gateway-route sender lane はいずれも
herdr-branch or gated no-op。send/capture/Enter (`run_tmux` / `capture_pane`) と select-pane activation は
#13253 shim 経由 (herdr port / no-op)。`window_active_pane_id` (pane_lines 読み) は send path から未到達。

backend=`tmux` 経路は byte 不変 (全 gate は `if not herdr_send` / `sender_lane_unit=None` で strict guard)。
fail-closed (un-attested sender env / unavailable inventory / 単一 live agent に解決しない) は structured
`blocked` / `target_unavailable` outcome を emit して die する。

### 残 residual (未確認事項)

- **live smoke 未実施**: 実 herdr binary + 実 agent での **end-to-end** round-trip (session-start →
  handoff marker landing / turn-start) は coordinator の post-review 実機 acceptance に委ねる。
  `agent start` の CLI 仕様自体は herdr 0.7.1 で実測済み (§5 launch contract)。本 US の自動テストは
  fake herdr runner のみ。
- ~~coordinator callback 経路の herdr 対応は別 surface~~ **RESOLVED (Redmine #13476, design consultation
  j#74599 Option A)**: send entry (`resolve_herdr_send_target`) が `--target coordinator` semantic
  pseudo-target を route authority に接続し、workspace default lane の coordinator provider へ解決する
  (§3.1 の「coordinator pseudo-target の send-entry translation」bullet)。`orchestrate_handoff` の
  `RECEIVERS` は `claude`/`codex` のまま (internal translation)。live smoke (実 herdr での coordinator
  callback round-trip) は coordinator の post-review 実機 acceptance に委ねる。
