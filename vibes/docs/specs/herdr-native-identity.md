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
   §lane_placement)。**未設定時は product default `--split down` を出す (Redmine #14568)**。
   default lane は `--tab` を出さないままだが `--split` は出す (両者は独立 flag)。tab root pane は
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
   **launcher target-authority compatibility preflight (Redmine #14258)**: #13748 / #13847 / #13882 の
   3 conjunct はいずれも launcher を **attestation store** に対して検証する。しかし launcher は自分が
   書かない authority を 2 つ **読む**必要があり、どちらの skew も lane を作った**後**に殺す。
   (a) **target repo の `.mozyo-bridge/config.yaml`** — wrapper は `--cwd <lane worktree>` で起動し
   mozyo-bridge CLI は startup でその config を parse するため、config schema bump に遅れた launcher は
   provider を exec する前に exit する (実測 j#85834: `unknown key 'agents'` / exit 2。`sublane create
   --execute` は worktree を作った後で両 slot が `provider_exited / rollback_owed`)。
   (b) **home-scoped shared lane lifecycle authority** — 最も新しい lane の source CLI が additive に
   migrate するため、reader が古い launcher は named lane を `LaneLifecycleReaderUpgradeRequired` で
   zero-start する (実測 j#85890: v7 store vs v6 reader)。
   検証手段は 2 つの authority で異なる。**lane lifecycle は宣言 join**:
   `agent-attest --help` epilog が `mozyo_attest_capability_lifecycle=<versions>` を wrap-proof
   token で advertise し、shared store の recorded component version と join する。probe は実 reader と
   **同一の known-signature authority** (`readonly_compatible_select`) を使う — metadata/version/table
   が整合していても column signature が壊れていれば実 reader は拒否するため、弱い認識で credit すると
   「reader が読める」を証明したことにならない (review j#87746 R2、実測: 健全な v7 store から
   `lane_kind` を落とすと旧 probe は `recognized/7`、実 reader は partial/corrupt で拒否)。

   **config は宣言でなく直接測定する**。grammar の *要約* は原理的に不足する: commit `d28e59e2` は
   supported version 集合も top-level key 集合も変えずに nested key `lane_placement.by_lane_kind` を
   追加したため、それ以前の launcher は同一 contract を advertise しながら当該 config を
   `unknown key 'by_lane_kind'` で拒否する (review j#87752 R4、実 pre-`d28e59e2` source で実測)。
   したがって epilog は `mozyo_attest_capability_config_parse=<contract version>` として
   「**訊いてよい**」ことだけを advertise し、答えは launcher 自身の parser から取る: preflight は
   join 対象の **exact bytes** を private temp file へ置き `<launcher> config check-parse --file <path>`
   (read-only、parse 成功 0 / 拒否 2) を実行する。これは version / top-level / nested / **将来追加される軸**
   を一度に覆う。判定は 2 つの測定の直積で、自 runtime も拒否 → `target_config_invalid`
   (config 自体の欠陥。launcher を責めない) / 自 runtime OK・launcher 拒否 →
   `launcher_cannot_parse_target_config` (launcher の実 error を引用) / 両者 OK → admit。
   #14231 の cwd-sensitive probe は「launcher が偶然その cwd で非 0 終了する」ことに依存する
   incidental discriminant であり、lane worktree が存在しないと問いを立てられない。宣言 join も直接測定も
   **worktree 生成前**に評価できるため、`sublane create` は worktree を残さずに拒否できる
   (close condition 1)。ただし worktree 生成前に config を読むには base を **immutable full commit へ
   一度だけ pin** しなければならない: 文字列 ref は pin ではなく、branch / remote-tracking ref は preflight と
   `git worktree add` の間に前進し得る (review j#87746 R1、実測: v2 config を admit した後 worktree には
   v99 が materialize)。`pin_base_commit` が最初の mutation より前に `rev-parse --verify <ref>^{commit}`
   で解決し、以降の config read と worktree 生成は**同一 object** を指す。解決不能 / ambiguous / 非 commit は
   zero-mutation refusal (ref へ fallback しない)。ambiguity の判定は **git 自身に委ねる**: この 1 command に限り
   `-c core.warnAmbiguousRefs=true` を強制し、「成功したのに stderr が非空か」だけを見る (文字列一致をしないので
   locale 非依存、ambient config でも無効化されない)。pseudo-ref (`$GIT_DIR/<name>`) の候補規則を自前で模写しない —
   模写は 2 度外した (解決順序の先頭欠落 j#87772 / casing による誤った一般化 j#87777。実際の境界は
   `.git/<name>` の**内容が ref として有効か**であって大文字小文字ではない)。この規則は ambiguity 以外の警告でも
   拒否する意図的な over-refusal であり、逃げ道は full SHA の明示 (常に無警告で pin される)。実際の conjunction (`preflight_launcher_compatibility`) は 2 箇所から呼ばれる:
   `sublane create --execute` の pre-mutation gate (worktree より前) と `prepare_session` (first herdr
   write より前、create を経由しない heal / 明示 `herdr session-start` で唯一到達する境界)。両者が
   **同一関数**を呼ぶのは、conjunct が片方にしか存在しない状態が live failure として再出現するため。
   両 authority は read-only で probe し、launch は shared authority を **migrate しない** (migrate すると
   同じ形で古い installed launcher を壊す)。**config axis は宣言 join ではなく直接測定である** — launcher
   自身の parser に exact target bytes を parse させ、自 runtime の verdict との直積で分類する
   (自 runtime も拒否 → `target_config_invalid` = config 自体の欠陥であり launcher を責めない /
   自 OK・launcher 拒否 → `launcher_cannot_parse_target_config`)。「宣言 version と top-level key のみを
   読み nested を見ない」旧記述は R4 で廃止した設計であり、要約では nested 追加を捕らえられない。
   lane lifecycle authority のみが宣言 join である。
   probe の repo 選択は **cwd 一軸に固定**する: `resolve_repo_root` は `--repo` > `MOZYO_REPO` > cwd の順で
   解決するため、probe env から repo 選択 env を除去しないと ambient `MOZYO_REPO` が neutral cwd を
   上書きし、直接測定へ到達する前に advertisement probe が target config で死ぬ (review j#87786 R10 実測)。
   public 証拠の **path redaction は allowlist ではなく positive proof** で判定する: 引用する text は
   candidate launcher の stderr であり形式は当方の管理下にないため、「既知の安全な delimiter が前置される
   場合のみ path と見なす」旧規則は列挙外の形 (`config:<path>`、backtick、brace、pipe) を素通りさせ、
   escaped same-quote では quoted run が途中で閉じて残りの private tail が公開された (review j#87824 R20、
   5 形すべて実測)。現規則は絶対 root の出現を既定で private と扱い、**語中の `/` (相対 token) のみ**を
   positive proof で保持する。proof は **occurrence 単位**で、周囲の token へは一切及ばない — token 全体へ
   広げると同 token 内の後続 root が黙って免除され、TAB / NBSP / `?file=` の後の private path が素通りした
   (review j#87831 R21、4 形実測)。
   `scheme://` URL は **保持しない**。URL の path / query / fragment は private path を運び得るが、それを
   通常の doc path と分ける構造的手がかりは存在せず、内容の模写でしか区別できない。したがって最初の
   `scheme://` 出現以降は**条件分岐なしに行末まで**を固定 placeholder へ畳む (design consultation j#87837 →
   j#87841)。URL の**終端規則を privacy authority に使わない**ことが要点で、終端を置いた 2 案はいずれも実測で
   破れた: Unicode whitespace 終端は `?file=C:\Users\<name>\private.yaml` を空白で切り root を失った tail を
   残し、「forward slash を含む URL のみ行末破棄」は drive / UNC path が forward slash を持たないため同 case を
   取り逃した。URL 後の prose と 2 個目の URL も落ちる。これは情報量を下げて close condition
   (private absolute path 残存 0) を満たす意図的な選択であり、URL は recovery authority ではない。
   target が unreadable / unsupported、launcher が capability を advertise しない場合はすべて
   fail-closed (unprovable は compatible でない)。config が存在しない repo は parse する対象が無いので
   admit する (この check 以前に動いていた case を defect 無しに壊さない)。
   **launch-generation protocol conjunct (Redmine #14203)**: 上の 4 conjunct はいずれも launcher が
   attestation を「書ける」ことを検証するが、parent の launch-generation finalize が join する
   `attestation_write_succeeded` startup execution event を wrapper が **emit するか**については何も
   述べない。`agent-attest` と一致する attestation schema を持ちながらこの event を持たない launcher は
   4 conjunct すべてを通過し、parent が generation を `pending` 予約 → pair 起動 → **actuation 後に初めて**
   finalize 不能が判明して generation が固定化し、#14203 の gateway recovery 自体を塞ぐ。したがって
   generation protocol は attestation schema から推定せず **独立の conjunct** として advertise / 判定する:
   epilog は `mozyo_generation_protocol_capability=<wire version>` を wrap-proof token で出し、preflight は
   step 1 で読んだ同一 advertisement から (追加 probe 無しで) exact version 一致を要求する。判定は
   conjunction の **最後**に置く — attestation 系 conjunct も落とす launcher は、より基底であるそちらの理由で
   拒否されるべきだからである。この conjunct を reserve boundary (`prepare_session`) だけに置くと、
   `sublane create` の pre-worktree gate を通過して worktree を作った後で拒否することになり、
   close condition 1 (worktree を残さず拒否) が破れる。conjunct を 2 boundary の片方にしか置かないと
   live failure として再出現する、という本 conjunction の存在理由がそのまま当てはまる。
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

## 5.1 lane_placement — pair 配置の設定駆動化 (Redmine #13646 / #14569)

herdr pane pair の **split 方向**・**役割順序** (どちらの provider が先 = 左 / 上に置かれるか)・
**pair 内部の相対 split 比率** (Redmine #14569) を lane class 別に宣言する closed config block。
`.mozyo-bridge/config.yaml`:

```yaml
lane_placement:
  default:                    # coordinator / auditor pair (bare `mozyo`)
    split: down               # right | down
    order: [codex, claude]    # exact permutation
    ratio: 0.6                # order[0] 側 (down なら上 / right なら左) の占有率
  sublane:                    # lane gateway / worker
    split: right
    order: [codex, claude]
    ratio: 0.5
```

### Schema (fail-closed)

- lane class key: `default` | `sublane` (`agent_launch` と同じ lane-class 軸。ただし別関心 =
  pane geometry であり、launch-argv token 軸とは resolve を分離し混同しない)。
- `split`: `right` | `down`。herdr 0.7.1 `agent start --split right|down` の語彙 (実 `--help` 照合)。
- `order`: `[codex, claude]` / `[claude, codex]` の **exact permutation**。欠落 / 重複 / 未知 provider /
  非 list は fail-closed (部分順序が silent に provider を落とさない)。
- `ratio` (#14569): **有限数値**で `0.1 <= ratio <= 0.9`。意味は常に **effective `order[0]` 側**が
  pair の split 全体に占める割合 (`down` なら上 pane、`right` なら左 pane)。bool / 文字列
  (`"0.6"`) / list / mapping / `nan` / `±inf` / 範囲外は **config parse 時に fail-closed** し、
  pane actuation へ到達させない。
- lane class object 自体・`split`・`order`・`ratio` はそれぞれ **個別に optional**。欠落した field は
  product default を継承する (#14568 / #14569。空 `{}` は「宣言しない」であって rollback ではない)。
- unknown class / unknown key / unknown value / unsupported version は fail-closed。

#### `ratio` の値域を herdr の実効 domain へ狭めた理由 (#14569、実測 j#91140)

herdr 0.7.4 の CLI parser は有限 `f32` を広く受理するが、**layout 側が split ratio を
`0.1..0.9` へ silent clamp** する (実測: ratio 0.5 の split に `pane resize --direction up
--amount 0.9` を適用すると 0.0 ではなく 0.1 に着地)。schema をこの実効 domain へ狭めない場合、
`ratio: 0.95` と宣言した workspace の pair は恒久的に 0.9 で描画され、**宣言値と effective layout が
乖離したまま誰も気づかない**。silent clamp に依存せず parse 時に拒否するのはこのためである
(Design Answer j#91127「silent clamp を採らない理由」)。

`ratio` は **相対比率のみ**で、固定 pixel 幅 / column 数 / row 数は宣言しない (#14569 Non-goals)。
herdr は split を ratio として保持し pane rect を container から再導出するため、相対比率は terminal
resize 後も維持・再現される。固定 extent はそれができない。

### Product default (Redmine #14568 — #13646 の未設定 byte-invariance を意図的に置換)

未設定時の既定は **`split: down` (default / sublane 両 class)**。`by_lane_kind` > lane class >
**product default** の 3 層の最下段であり、宣言が無い workspace でも pair は縦分割される。

| lane class | product default `split` | product default `order` | product default `ratio` | 上段になる provider |
| --- | --- | --- | --- | --- |
| `default` | `down` | `[codex, claude]` | `0.5` | codex (coordinator) |
| `sublane` | `down` | 宣言しない (要求順を維持) | `0.5` | gateway (既定 binding では codex) |

`ratio` の product default `0.5` は **#14568 の supersession を拡張しない** (#14569)。herdr が
fresh split を等分で作るため、`ratio` を宣言しない workspace の landing geometry は本軸の追加前後で
同一である。`split` の既定変更 (#14568) が意図的な geometry 変更だったのに対し、`ratio` 軸の追加は
**未宣言 workspace に対して geometry-preserving** である。`order` と違い `ratio` は尊重すべき role
binding を持たない (provider の選択ではなく split の配分である) ため、`sublane` でも宣言しない理由が無く、
両 class に同じ `0.5` を置く。

`order` を lane class で非対称にしているのは、**片方だけが尊重すべき role binding を持つ**ため。

- `default` = bare `mozyo` の coordinator pair は固定 topology
  (`default_agent_topology.DEFAULT_EXPECTED_AGENTS`) を launch し、その launch 順は `claude` 先である。
  縦分割すると implementer が上段になるので、product default で `[codex, claude]` に pin する。別 binding
  の workspace は `lane_placement.default.order` を明示宣言する。
- `sublane` は既に role binding から解決した `(gateway, worker)` 順で launch する
  (`sublane_actuator_herdr_ops._launch_providers`)。ここに product default `order` を置くと binding を
  **尊重ではなく上書き**してしまうため、宣言しない。結果として gateway が上段になり、rebound binding は
  その binding 順のまま launch される。

#### 互換性と rollback (adopter 向け)

- **既存 live pair は暗黙再配置しない**。product default は fresh launch / heal の argv を決めるだけで、
  live pane を move / swap / kill しない。既に左右で立っている pair は次の fresh launch まで左右のまま。
  live で今すぐ変えるなら [[logic-herdr-live-relayout-runbook]] の手順を使う (境界は不変)。
- **rollback は `split: right` の明示宣言**。粒度は 3 つあり、いずれも従来どおりの優先順で効く。

  ```yaml
  lane_placement:
    default:
      split: right          # coordinator pair だけ左右へ戻す
    sublane:
      split: right          # 全 sublane pair を左右へ戻す
    by_lane_kind:
      implementation:
        split: right        # 孫 lane だけ左右へ戻す (lane class より優先)
  ```

- **確認手段は `mozyo-bridge config status`**。`lane_placement.<class>.{split,order,ratio}` の leaf row が
  effective 値と `declared` / `default` の別を出すので、宣言していない workspace は
  `lane_placement.default.split = down (default)` / `lane_placement.default.ratio = 0.5 (default)` として
  読める。この row は launch chokepoint と **同じ resolver** (`LanePlacementConfig.resolve_effective`) を
  読むので、status と実 launch は乖離しない。`ratio` は **実機を測らないと読み取れない唯一の placement
  field** なので、この row の存在が確認手段そのものである。
- 設定した field だけが差分を生む。`default` を設定しても `sublane` の launch は不変 (逆も同様)。
- `by_lane_kind` の **wholesale shadowing は不変** (#13647): 宣言された kind は lane class を丸ごと
  shadow するため、`order` だけ宣言した kind は lane class の `split` を継承せず product default を取る。
  #14568 も #14569 もこれを per-field merge に変えていない (`ratio` だけ宣言した kind は lane class の
  `split` を継承せず、`ratio` を宣言しない kind は lane class の `ratio` ではなく `0.5` を取る)。

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
  → **fresh container では 1st launch を `--focus`** にして split target を 1st agent へ固定し、2nd 以降は
  `--split <dir> --no-focus` とする。root pane reclaim は従来どおり **全 launch 成功後**
  (partial-launch safety を壊さない)。
  発火条件は: container occupancy = 0 かつ launch 対象 2 件以上かつ **effective split 方向が非空**。
  **single-provider / heal / mixed adopt では発火しない**。
  なお `--split right` literal (#13411) も同じ理由で本来効いておらず、観測される `right` は herdr 既定
  split の偶然の一致だった。
  **#14568 の変更点**: 発火条件の第 3 項は「`split`/`order` が **explicit**」から「**effective** split 方向が
  非空」へ移った。product default で全 lane class が `down` を持つ以上、「operator が宣言したか」を
  問うと未宣言 workspace だけが focus を得られず、argv には `--split down` が出るのに実機は
  reclaim で `right` へ畳まれる — 本 fix が防ぐはずの症状そのものになる。
- **single-provider request**: `order` は **未要求の peer を暗黙 launch しない**。heal は欠けた provider
  だけを launch する。
- **heal**: 生存 sibling の隣へ configured `--split <dir>` で launch する。既存 pane は swap / move /
  bounce / focus しない (container の唯一の pane = 生存 sibling が既に split target なので `--focus` 不要)。
- **order best-effort**: herdr `agent start` に pane-target flag は存在しない (実 `--help` 照合) ため、
  役割順序は **launch 順としてのみ**実現できる。configured primary (`order[0]`) が後から復旧し、生存
  sibling の隣へ split するしかない場合は物理順序を満たせないので、その slot の `detail` に
  `order_deferred_until_full_relaunch` を出す (silent に「order 適用済み」と主張しない)。full relaunch で
  configured order が物理的に実現する。

#### ratio actuation (Redmine #14569、実測 j#91140)

herdr 0.7.4 `agent start` は **`--ratio` を持たない** (`pane split` / `pane move` は持つ)。したがって
`ratio` は launch argv には乗らず、**全 launch 成功 + root pane reclaim の後**に herdr-native
`pane resize --amount` で 1 度だけ actuate し、`pane layout` で **測って** 判定する。正本実装は
`herdr_pair_split_ratio.finalize_container_geometry` (reclaim と ratio を 1 つの後処理にまとめた
cohesive sibling。reclaim が先なのは、root pane を閉じると split tree が畳まれ、その前に測った
geometry が直後に変わるため)。

- **actuate するのは「この run が今作った divider」だけ**。判定は
  `launched >= 1 かつ (container の初期 occupancy > 0 または launched >= 2)` — `--split` を出した launch が
  この run に 1 つ以上あることと同値である。all-adopt / dry-run / 空 container への単発 launch は
  divider を作らないので **何も触らない**。config を読むだけで live pair が動く経路は存在しない。
  **この述語が「測定するか」も決める**: これを通過した run は測定を負っているので、以降の拒否はすべて
  `failed` であり `not_applicable` ではない。`not_applicable` になるのは **layout を 1 度も読む前に**
  決まる場合 (dry-run / divider 未作成 / ratio・direction 未解決) だけである。
- **pair は「この run が split した slot」を起点に layout から読む**。起点はその run が launch した
  slot のうち最後の 1 つ (occupancy が最大なので必ず `--split` を出している)。sibling が **この run の
  slot である必要は無い** — `replacement_target_only` の single-provider heal は 1 slot しか持たないが、
  生存 sibling の隣へ split して divider を作るのだから測定対象である (review j#91217 R1-F1: 旧実装は
  「pair の 2 pane が両方この run の slot」を要求したためこの heal を丸ごと素通りさせ、宣言 ratio を
  適用しないまま成功と報告していた)。
- **対象 split の同定は幾何で行う**。`pane layout` の `splits[]` は child pane id を持たないため、
  「起点 slot と他の 1 pane がその split rect を **exact tiling** するか」で同定する。単純な
  bounding-box 一致は誤判定する (実測: 3 pane の nested layout で、兄弟でない 2 pane の union rect が
  root split の rect と一致した)。候補が 0 個または 2 個以上なら fail-closed。
  herdr は split した pane を **second 側**へ置くため、起点 slot が first 側に居る layout は
  予測しない形として fail-closed とする。
- **shared tab 安全弁 (#14567 との組合せ)**: herdr の `pane resize` は「指定 pane の、direction と軸が
  一致する最も近い祖先 split」を動かす。したがって actuate 前に「その最近祖先 split が pair 自身の
  divider か」を照合し、一致しない場合は **resize を発行せず fail-closed** にする。これが無いと、
  全 sublane を単一 tab へ集約した構成で外側の divider を動かし隣の lane を再配置しうる。
- **収束と検証**: `--amount` は 1 回あたり 0.5 に clamp される (実測) ため、観測 ratio から毎回 delta を
  再計算する有界ループ (最大 4 pass、進捗が止まったら中断) で寄せ、最後に `pane layout` を読み直して
  判定する。判定は 2 本立てで、split ratio が宣言値と `f32` 誤差内 (`1e-3`) であること、かつ first-child
  pane の extent が `round(extent * ratio)` の ±1 cell 以内であること。**resize が exit 0 で返ったことは
  根拠にしない** (herdr は amount も ratio も silent clamp するため)。
- **outcome は独立軸**として `SessionStartResult.ratio_outcome` に出す (`not_applicable` / `matched` /
  `applied` / `deferred_until_full_relaunch` / `failed`)。`ratio` は 2 pane の **divider** の性質であり
  どちらの agent の health でもないので、slot health に畳まない。
- **order-deferred heal**: configured `order[0]` が物理的に 2 番目 (生存 sibling の隣) に着地した場合、
  そこへ `ratio` を適用すると `order[0]` の取り分が `order[1]` に渡る。live pane の swap / bounce は
  禁止なので **適用せず `deferred_until_full_relaunch` を明示**する (どちらも主張しない)。full relaunch で
  order と ratio が同時に実現する。deferral は失敗ではない (pair は使用可能)。
  判定は **split した slot の provider が effective order の先頭であるか**による — これは
  `slot_placement` が `order` 軸に対して既に使っている規則と **同一**である (ratio 専用の第二の
  「primary」定義を作らない)。sibling の provider は判定に不要である。

  **effective order の解決 (Redmine #14569 review j#91263 R2-F1)**。**3 つの authority 層 + 終端 fallback**
  で解決する (層は 1〜3、4. は「どの層も答えられなかった」という終端であって 4 番目の authority ではない)。

  各層は **canonical provider の exact permutation として受理できる場合のみ**採用し、答えられない層は
  半端に答えず読み飛ばす (値域は宣言 `order` と同一 = `lane_placement._normalize_order`)。

  1. 宣言 (または product default) された `order` — primary を直接名指しする。
  2. 無ければ **その run の lane が持つ stable な managed pair 順**。**`order` 未宣言は「順序主張が
     無い」ではない** — sublane で `order` を宣言しないのは binding が解決した `(gateway, worker)` 順を
     **尊重する**ためであり (§5.1 Product default の非対称性の理由、j#91127)、その順序は本 block の
     上位で解決される。したがって `prepare_session` は caller から `pair_order` として受け取る。
  3. 無ければ **その run 自身の要求 providers**。要求が full pair である限りそれが pair 順そのもので
     あるため、通常経路は caller からの供給を必要としない。**縮小された要求は permutation にならないので
     何も寄与しない** — 縮小 list の中ではその 1 provider が自明に「先頭」になり、それが誤帰属だからである。
  4. (終端) どの層も答えられなければ空 — 帰属できない (`unattributable`) → deferral。

  **`pair_order` は最初の副作用より前に検証する** (`validate_pair_order`、review j#91284 R3-F1 /
  j#91331 R4-F1)。未知 provider / 重複 / 欠落 / 非 string 要素 / 非 sequence、および
  **要求 providers を含まない order** は zero-side-effect で拒否する。
  **「boundary に置く」だけでは足りない**: public `prepare_session` は attestation store lock を先に取得し、
  その取得が mozyo home directory と lock file を作る。したがって検証は **lock 取得より前**に走らせる
  (caller-held lock 経路の `_prepare_session_locked` 側でも重ねて検証し、どちらの入口も拒否する)。検証していない値を authority として
  受理すると実害が出る: `pair_order=("unknown","codex")` は `codex` を primary でなくするため、gateway の
  target-only heal が pair を resize して **gateway の宣言 share を生存 worker へ渡したまま `applied` と
  報告**した (実測 j#91299)。resolver 側も `str()` coercion をやめ、認識できない入力は空 = deferral に倒す
  (誤った分割より必ず安全側)。

  第 2 層が必要なのは、**target-only replacement が要求を 1 provider へ縮小する**ためである
  (`replacement_target_only` → `startup_providers = (provider,)`)。縮小後の要求はもはや pair 順ではないので、
  それを effective order として読むと gateway を heal したときに生存 worker が first 側になり、**宣言 share が
  逆の role へ渡ったまま `applied` と報告される** (R2-F1 の実測)。縮小した caller が stable な順序を渡す。
  空 (縮小されたのに stable 順序が渡されなかった) の場合は **deferral 側へ倒す** — 縮小 list の中では
  その 1 provider が自明に「先頭」になり、それこそが R2-F1 の誤帰属だからである。
- **失敗は成功扱いしない**: layout の read / parse 失敗、pair split の同定失敗、`pane resize` の拒否、
  最終照合の不一致はいずれも `failed` とし、`SessionStartResult.ok` を False にする。ただし
  `owes_rollback` には入れない — 分割が意図と違うだけの pair は使用可能であり、それを理由に agent を
  kill / close する方が有害だからである (`ratio` 経路は pane を一切 close しない)。

### Boundary

- **tab topology とは直交** (#14567 との境界)。`lane_placement` が決めるのは *container の中で pair を
  どう割るか* だけで、*どの container に入るか* (workspace / tab) は #13380 / #13411 の join 軸が決める。
  よって #14567 が全 sublane を単一 tab へ集約しても、その tab の中で各 lane pair は本 block の
  `split` / `ratio` に従って置ける。両者を組み合わせる時に本 block を変更する必要は無い。**lane 間の
  配置比率と pair 内部の比率を混同しない**: 本 block の `ratio` は 1 pair の divider だけを指し、lane
  同士の列幅は #14604 の scope である。実装上その混同を防いでいるのは上記 shared tab 安全弁で、
  actuate 対象が pair 自身の divider でないと分かった時点で resize を出さない。
- `lane_placement` は **future launch policy** であり、live layout / liveness / route authority ではない。
  config を読むだけで既存 live pair を移動 / 再分割しない。`ratio` (#14569) が pane を 1 度 resize するのは
  **その run 自身が今作った divider** に対してだけで、既存 live pair の divider には触れない
  (herdr は same-tab re-split を拒否する。live 再配置・live での比率変更は operator の CLI 操作のまま:
  [[logic-herdr-live-relayout-runbook]])。
- config key は `pane_placement` では **なく** `lane_placement`。repo-local schema boundary
  (`_FORBIDDEN_KEY_PARTS`) は `pane` を含む key を allowed-key 判定より前に拒否するため、live pane
  addressing に見えない名前へ寄せている (boundary screen は緩めない)。

### 拡張点 (#13646 v1 非対象 → #13647 で解消)

owner の「親子孫 3 層それぞれで変えたい」要望のうち、layer 別 (親 / 子 / 孫 lane role 別) の key 分けは
**#13646 v1 には含めなかった**: launch 経路の語彙が lane_class (`default` / `sublane`) の 2 値しか無く、
layer 別 keying には lane-role 語彙の launch 時解決が別途必要だったため。予告どおり既存 class の意味論を
変えない additive 拡張として **#13647 が `by_lane_kind` block を追加**した (下記 §5.2)。

## 5.2 by_lane_kind — lane-role (親 / 子 / 孫) 別 pane 幾何 (Redmine #13647)

§5.1 の lane_class 軸と **disjoint な additive 軸**。同じ `sublane` class の中で 子
(`delegated_coordinator`) と 孫 (`implementation`) に異なる split を与える。

```yaml
lane_placement:
  sublane:                       # lane class 軸 (#13646) — そのまま有効
    split: right
  by_lane_kind:                  # lane role 軸 (#13647) — additive
    coordinator:            { split: down }
    delegated_coordinator:  { split: down }
    implementation:         { split: right, order: [claude, codex] }
```

### Vocabulary (fail-closed、alias 無し)

- key は canonical 3-token `coordinator` | `delegated_coordinator` | `implementation` **のみ**
  (disposition j#85650 P3)。`parent` / `child` / `grandchild` / `coordinator_assistant` は parse 時に
  `LanePlacementError`。owner 向け表示が 親/子/孫 を使うことと machine vocabulary の拡張は別。
- `split` / `order` の語彙・fail-closed 規律は §5.1 と同一。block 自体は optional。

### Precedence

`by_lane_kind[kind]` > `lane_class` > **product default** (#14568。#13647 時点の表記は「legacy 既定」)。
kind 層が参照されるのは **durable な lane_kind が解決でき、かつ config がその kind を明示宣言している
時だけ**。未解決 kind / 未宣言 kind / block 不在はすべて §5.1 の lane-class 解決へ fall-through する。

3 層の合成は `LanePlacementConfig.resolve_effective` が単独で持ち、launch 経路
(`resolve_placement_policy_for_role`) と `config status` の双方がそれを読む。どちらかが層を再実装すると
「status の表示」と「実 launch の幾何」が乖離しうるため、resolver は 1 つに保つ。

宣言された kind は lane class を **丸ごと** shadow する (per-field merge ではない)。したがって
`order` だけ宣言した kind の `split` は lane class ではなく product default を取る。この shadowing 規律は
#14568 でも変えていない。

### lane_kind の 2 authority (Redmine #13647 Tranche 1a / 1b)

- **fresh launch** = caller-supplied `LaneLaunchContext` (pure immutable value)。create / heal
  boundary で **創出側 caller が governance から解決**して `prepare_session` へ渡す。bare `mozyo`
  launch と no-lane `herdr session-start` は構造上 `coordinator` を渡す。`sublane create --lane-kind`
  は創出側 coordinator の宣言を運ぶ。
  **lane の初回 launch では context が唯一の authority である** (review j#85848 F1): create 経路は
  lane の lifecycle row を **launch が返った後**に declare するため、初回 launch 時点に stored kind は
  存在しない。context を渡さないと「pane を実際に作る launch」だけが `lane_class` 幾何になり、以後の
  heal だけ設定どおりになるという逆転が起きる。よって actuator は `prepare_actuator_lane_session`
  (create / heal / v1 replacement が通る唯一の funnel) へ pre-launch に context を渡す。
- **heal** = lane lifecycle authority row の generation-bound `lane_kind` (schema v7、
  `managed-state-model.md`)。launch chokepoint が **network 無し / display cache 無し**で offline 読解する。
- **矛盾は fail-closed**: 両方あって不一致なら片方が stale。launch admission (#14242 の
  disposition admission と同じ pre-side-effect boundary、同一 snapshot) が **workspace / tab / agent を
  1 つも作らずに拒否**する。再結線は generation 境界 (`open_next_generation(lane_kind=...)`) のみ。
- **stored token は read 境界で canonical validation する** (review j#85848 F2): 空 = 「durable kind
  fact 無し」= 唯一の正当な不在で `lane_class` fallback。空でない **非 canonical 値**(改竄 row /
  foreign writer / 将来 build の語彙)は「解釈できない authority 値」であり、**不在として扱わず**
  副作用前に launch error にする (j#85650「invalid / ambiguous は zero-start、blank のみ fallback」、
  および本 component の "unreadable is not absent" 規律)。
- provider / pane 近接 / `lane_metadata` などの display 由来値から lane_kind を推測しない
  (disposition j#85650)。`--dry-run` は durable state を一切参照せず caller context だけで plan する
  (#13595 / #14242 と同じく dry run は store-free)。

## 5.2.1 ResolvedLaneLaunchPlan — whole-plan preflight (Redmine #13647 Tranche 2)

§5.2 が pair の **幾何**を決めるのに対し、こちらは **per-slot の責務**を launch 前に一括で
固める。caller が slot ごとに `workflow_role` / `profile_id` / `provider` /
`resolved_launch_argv` / `physical_slot` を供給し、pair 全体を 1 つの plan として検証してから
最初の不可逆操作へ進む (Design Answer j#85645 「whole-plan preflight」)。

### なぜ pair 単位か

launch は pair を作る。slot を launch しながら個別検証すると、2 番目で異常を見つけた時点で
1 番目は既に live = **partial lane** になる。さらに「同一 role を 2 slot が主張」「1 つの
physical slot に 2 entry」「同一 slot に別 profile」といった欠陥は **plan 全体を見ないと不可視**。
したがって plan が検証単位であり、下記はすべて **typed zero-start** (workspace / tab / agent /
startup action を 1 つも作らない)。

### 検証項目 (すべて zero-start)

1. slot の `workflow_role` / `profile_id` / `provider` / `launch_argv` が未解決
2. 未知の `workflow_role` / 未登録 `provider` — **default へ degrade せず拒否**
3. `workflow_role` の重複 (同一責務を 2 slot が主張)
4. 同一 `physical_slot` への複数 entry / **`physical_slot` が空** (空値は「未指定」ではなく
   衝突検査の抜け穴。plan は各 slot の pair 位置を明示する)
5. 同一 `(physical_slot, provider)` に **異なる profile / argv** (provider が違えば正当ゆえ
   same-slot 衝突のみ拒否)
6. governance anchor が **0 件または ambiguous**。非空 plan は **distinct anchor ちょうど 1 件**
   を必須とする (同一 record の重複は 1 件へ集約)。責務を割り当てる plan がその割当を行った
   durable decision を名指せないなら、推測由来の plan と区別できないので launch しない。
   `slot_specs` 空 (= plan を作らない従来 caller) だけが anchor 不要
7. **plan が「この launch」を過不足なく説明していない** — slot 数 / provider multiset が実
   request と不一致 (partial / extra / request 外 provider)。plan が 2 slot 中 1 slot しか
   説明しないまま launch すると、残る slot は「何者か誰も宣言していない」状態で live になる
   = 本 gate が防ぐはずの partial lane そのもの

launch **順序**は照合しない: placement 解決 (`resolve_launch_order`) が本 preflight の**後**に
provider を並べ替えるため、順序一致を課すと正しい plan を誤って拒否する。個数 + multiset +
位置一意で「pair を過不足なく説明する」を担保する。

### 検証済み plan の不変性

`frozen=True` は**属性の再束縛しか防がない**。caller が渡した list をそのまま保持すると
**検証後に中身を書き換えられ**、「最初の write の前に固定する」という本 gate の前提が崩れる。
したがって plan と slot は **外部から受け取る sequence をすべて construction 時に所有 tuple へ
copy** する:

- `SlotLaunchSpec.launch_argv`（slot が実行する command）
- `ResolvedLaneLaunchPlan.slots`（pair の構成そのもの）
- `ResolvedLaneLaunchPlan.placement` の order sequence（**launch 幾何** = どの provider が
  container を占めるか。検証後に変わればどの pane が先に置かれるかが変わる）

copy は resolver 経路だけでなく **public constructor 経路でも**行う（型は公開されており、
resolver を経ない構築も正当な入口であるため）。`source_anchor` は `DecisionPointer`（frozen・
scalar のみ）なので追加の copy は不要。

### 構造 validation と 文脈 validation の分離

type annotation は runtime 検査ではなく、`frozen=True` は属性の再束縛しか防がない。したがって
**value object 自身が自分の field を construction 時に検査する**:

- **構造 (structural) validation — 全 construction path で常に実行**:
  `SlotLaunchSpec` の `workflow_role` / `profile_id` / `provider` / `physical_slot` は `str` 必須。
  `ResolvedLaneLaunchPlan` の `lane_class` は `str`、`lane_kind` は `str` か `None`、
  `source_anchor` は `DecisionPointer` か `None`（「それらしい文字列」は governance provenance に
  昇格しない）。sequence は上記のとおり所有 copy し、要素型不正も拒否する。
- **文脈 (contextual) validation — `resolve_lane_launch_plan` のみ**: 語彙 membership
  (role / provider)、cross-slot の一意性、実 launch との照合、anchor exactness。これらは
  resolver にしか渡されない入力を必要とする。

したがって **直接構築した plan は「型として妥当」だが「この launch に対して validated」ではない**。
「validated plan」と呼べるのは resolver が返した plan だけである。

### 注入 vocabulary と order-bearing container

- **注入 vocabulary（role / provider / lane class / split）も public input** であり、context
  validation の前に検査する。とくに **bare string を渡されると `in` が substring 判定**になり、
  fail-closed 語彙検査が見かけだけ残って無効化されるため拒否する。非 iterable・非 str 要素も
  同様（後者は refusal message の join で raw error に化ける）。
- **順序を持つ field（argv / slots / placement order / launch provider list）は ordered
  sequence のみ**受理する。set などの unordered container は iteration 順が値の一部ではなく、
  同じ plan が別 process で別の argv / launch 順に固定されうるため typed refusal。
- **geometry 値自体の resolved 性**も resolver が検証する: `lane_class` は closed set、`split`
  は `right|down` か未指定、order の各要素は既知 provider で重複なし。**request の launch 順と
  照合はしない**（placement 解決は preflight の後に走るため。§5.2.1 検証項目の注記と同じ境界）。
  closed 語彙は application 合成点から注入する（pure leaf が config context を import しない）。
- **authority carrier `LaneLaunchContext` も同じ規律**を自分の field（`anchors` 要素 =
  `DecisionPointer`、`slot_specs` 要素 = `SlotLaunchSpec`、container は ordered sequence）に
  適用する。plan resolver 側の guard は独立に残し、双方に個別 test を置く（片方が他方の欠落を
  隠さないため）。

### 単一評価（check したものを store する）

caller から受け取った値は **境界で 1 回だけ読み、検証した *その値* を格納**する。同じ入力
オブジェクトを検証用に 1 回・格納用にもう 1 回読むと、読むたびに値が変わる入力（stateful な
sequence / property）に対して **検証済みでない値が「validated plan」に入る**（time-of-check /
time-of-use）。実測でも、1 回目に `("right", ("claude","codex"))`・2 回目に
`("diagonal", ("foreign",))` を返す placement が、closed 語彙検査を通過したまま後者を格納した。

対象は plan / context が受け取る全入力（`placement` / `slots` / `anchors` / `launch_argv` /
注入 vocabulary / launch provider list）。**空 plan の legacy 分岐も同じ規律**に従う（早期
return するため独立に担保が要る）。

### 単一 typed error

本境界が拒否するものは（構造・文脈・anchor いずれも）すべて `LaneLaunchPlanError` として送出する。
launch 側は単一の `except` でそれを typed zero-start に変換するため、別の例外型で抜けると
「typed zero-start」という公開契約が破れる（lane-kind 語彙違反もこの型へ再送出し、原因は
`__cause__` に保持する）。

### 境界

- `workflow_role` / `profile_id` は **plan-only**。mzb1 assigned name /
  `MOZYO_AGENT_ROLE` (= provider token) / route / attestation / retire identity へ昇格しない
  (j#84266)。
- role は**推測しない**。durable governance から一意に解決した caller だけが供給し、供給された
  が未登録なら fallback せず zero-start。`coordinator_assistant` を偽の `implementer` へ写像
  しない (本 US non-goal、別 issue)。
- anchor 語彙は lifecycle authority record と同じ `DecisionPointer` を再利用する (並行語彙を
  作らない)。
- **本 tranche の gate は「拒否」しかしない**: plan を argv 構築へ合成するのは後続 tranche で
  あり、valid な plan を渡しても launch argv は plan 無しと byte 一致する。
- `slot_specs` 未指定 (既存の全 caller) は検証自体を行わず、pre-#13647 と byte 一致。

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
write ではない (`flock` のみ)。lock lifecycle 全体で raw `OSError` を出さない: acquire error (fcntl 不能 / home permission /
`LOCK_EX`) は list/create の**前**なので **zero herdr actuation** で、release error (`LOCK_UN` / `close`、両方必ず
試行し fd は必ず close) は **body 成功時のみ**、いずれも session-start の typed error (`HerdrSessionStartError`) へ
変換して fail-closed する (R6 review j#83569 F2 / R7 review j#83596 F1、public CLI が raw traceback にならない)。
**body が例外を投げた場合は release error で上書きせず元例外を不変伝播**する (`_FenceLock.__exit__` と同 pattern)。
release error は acquire error と**別 subtype** (`CoordinatorSharedCreateReleaseError`) で、session-start は phase-accurate に報告する
(R8 review j#83633 F1): acquire failure は「zero workspace/tab/agent create」、release failure は body の後なので
「labelled `coordinators` workspace は作成済みの可能性があり agent 未起動、re-run が idempotently adopt」。
concurrent 収束は `threading.Barrier` + 共有 fake backend + `fcntl.flock` の別-fd 競合で **create count 1** を
deterministic に regression 固定する (live Herdr smoke 不要)。

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

### 高レベル isolated smoke harness (Redmine #14187)

`shared_space` の実 cross-process 経路 (実 `coordinators` workspace create + coordinator pair launch/adopt +
concurrent single-flight 収束 + teardown) を、raw Herdr (`HERDR_CONFIG_PATH` / `herdr server` / 手動
`herdr workspace ...`) を使わずに隔離・観測・cleanup できる高レベル surface を
`e_140_adapter_provider/f_130_terminal_runtime_provider/application/shared_space_smoke_harness.py`
(`SharedSpaceSmokeHarness`) に置く (#14185 Review j#83785 の blocker 解消)。これは **新規 diagnostic surface で
あり、上記の placement 契約・resolver・fence を一切変更しない**: 同じ `prepare_session`
(`coordinator_placement_mode=shared_space`, default lane) を injected `runner` 越しに駆動し、`_shared_coordinator_target`
resolver と `coordinator_shared_create_lock` fence をそのまま使う。

- **isolation (Acceptance 1/5)**: 実行前に `prove_smoke_isolation` が isolated home を実 operator home と
  distinct かつ非 nested と証明し (不能なら create 前 fail-closed)、`isolated_smoke_home` が `MOZYO_BRIDGE_HOME` を
  isolated home に向け、operator placement facade (`coordinator-placement.yaml: mode: shared_space`) を isolated home に
  書いて loader で round-trip 検証する。実 operator home / config は変更しない。
- **clean-slate cleanup-authority (Acceptance 5、herdr 次元)**: `coordinators` label は herdr server global なので、
  actuation 前に read-only `workspace list` で **既存 `coordinators` space 不在**を証明する。存在 / labels unreadable は
  create 前 fail-closed (実 operator space を adopt / 汚染しないため)。
- **observation (Acceptance 4)**: `RecordingHerdrRunner` が command 種別と非秘匿 identity token (`coordinators` label /
  `mzb1_...` name / `wN:pM` handle) のみ記録し、`--env` 値 / home path / payload 全文は記録しない。evidence 要約は
  count / bool / closed phase token のみ (durable journal 安全)。
- **concurrent 収束 (Acceptance 3)**: `run_concurrent` が project ごと 1 thread を `threading.Barrier` で同時 release し、
  isolated home 共有で実 `coordinator_shared_create_lock` を競合させる (§5.1.1 create fence と同じ deterministic 手法)。
  **create count 1 / duplicate agent 0** を実測する。orthogonal な #13948 startup-transaction fence は project ごと
  per-fence 隔離する (収束 test を coordinator create lock に集中、R7 j#83573 と同方針)。
- **cleanup + residue (Acceptance 5)**: launch した exact pane handle のみ close し (workspace は最終 pane で auto-vanish、
  #13380)、`workspace list` / `agent list` を読み返して residue 0 を証明する。generic kill は行わない。

unit / integration は共有 fake (`support.herdr_fake.FakeHerdr`、face H `workspace list` を追加) で駆動し、実 live smoke
(実 herdr binary + disposable instance) は Review 承認・integration・CI 後に #14185 が同 `SharedSpaceSmokeHarness` を
真の `multiprocessing` driver で再駆動して行う。CLI `mozyo-bridge herdr smoke-shared-space --isolated-home PATH` は
**read-only preflight** (isolation + clean-slate 証明) のみで agent を actuate しない。

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
