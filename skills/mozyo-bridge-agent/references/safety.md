# Safety リファレンス

## Secret の取り扱い

- 実物の PyPI/TestPyPI token、API key、個人 credential、個人情報を commit したり貼り付けたりしない。
- `.env`、`.env.*`、`.pypirc` は local 専用の secret surface であり、ignore されたままにしなければならない。
- secret を ticket システムの entry (Asana task description / comment、Redmine issue description / journal)、preset rule doc、knowledge base page、repository docs のいずれにも保存しない。

## 通知の安全規約

- `mozyo-bridge` は通知 transport である。
- review state、task 完了、release 承認の正本ではない。
- 受信側 agent は行動する前に Asana または指名された正本を確認しなければならない。durable な Asana / Redmine anchor が利用可能なときは、`mozyo-bridge status` 出力、`mozyo-bridge doctor` 出力、pane scrollback から receiver state、task state、gate state を推測しない。これらの surface は operator/debug 補助であり、durable record ではない。代わりに指名された task / comment / issue / journal を読む。
- 標準の handoff/reply 経路は高レベル primitive である: `mozyo-bridge handoff send` / `mozyo-bridge handoff reply` / top-level alias `mozyo-bridge reply`。この primitive は receiver pane を解決し、決定的な Layer B preflight を実行し、marker 前置の通知を type し、Enter を押す (queue-enter / standard rail) か pending のまま残す (`--mode pending`)。caller は通常の handoff/reply のために `mozyo-bridge read` + `mozyo-bridge message` の shell choreography を自前で組み立てない。
- `notify-*` wrapper (`notify-codex`, `notify-claude`, `notify-codex-review`, `notify-claude-review-result`) は互換 entrypoint であり、標準の Redmine 形式通知について内部で同じ primitive を経由し、同じ safety rail を維持する。`notify-*-legacy-task` は retired-queue cleanup 専用の wrapper であり、標準経路ではない。
- 低レベルの `mozyo-bridge read`、`mozyo-bridge message`、`mozyo-bridge type`、`mozyo-bridge keys` command は operator/debug primitive である (pane 検査、ad-hoc な operator message、raw typing、raw keys)。これらは標準の handoff/reply 経路ではなく、primitive の日常的な代替として手で組み立てない。認められた用途は、per-preset の Retry Path Checklist にある `--no-submit` operator/debug fallback と、明示的な operator debugging のみである。
- raw な tmux pane mutation は、workflow の delivery / recovery のための agent 操作として禁止である。agent は `tmux send-keys`、`tmux paste-buffer`、直接の Enter / `C-u`、その他の raw key injection で agent pane を操作しない。低レベルの `mozyo-bridge type` / `mozyo-bridge keys` を handoff の代替として使わない。`mozyo-bridge message --no-submit` を実行してから自ら raw な Enter を発行して submit することもしない。read-only の検査 (`tmux capture-pane`、`mozyo-bridge read`) は許可される。
- raw な tmux mutation は operator/debug 専用である。使用時点での明示的な operator 指示と、その指示の durable record (Redmine journal / Asana comment) が揃っている場合にのみ許可される。agent は自身の判断でこれを実行しない。
- `mozyo-bridge handoff send` / `handoff reply` が失敗した場合 (例えば `marker_timeout`)、agent は `un-notified` state を durable record に記録し、receiver への到達は承認済みの高レベル経路に委ねる。receiver pane を mutate して delivery を自己修復しない。
- 送信 rail は 2 本存在する。どちらかを弱めるのではなく、正しい方を選ぶ:
  - `--mode queue-enter` (agent pane handoff の v0.4 規範 default — Claude / Codex pane を target とする `mozyo-bridge handoff send` / `handoff reply` / `notify-*` 標準 variant。`--force` を拒否する): typing の前に決定的な Layer B preflight が走る。次の check のいずれかが失敗した場合、CLI は `send-keys -l` を発行する前に `blocked` と対応する `Reason` を出して die する:
    - 明示的な `--target` は receiver の tmux window 内に存在しなければならない (`Reason: invalid_args`)、
    - target pane は **送信側** の tmux session 内に存在しなければならない。すなわち receiver と同じ tmux session の内側から呼び出す (`Reason: invalid_args`)、
    - target pane はその window の **active split** でなければならない。**または** standard_target_admission (Redmine #12597) を pass する: 最小の admission 契約 (live pane / 強い role 一致 / `workspace_id` あり / 一意) を満たす registered な inactive split は、`tmux select-pane` で activate して deliver される — pane selection のみで、raw な `send-keys` / `paste-buffer` / 低レベル `type` / `keys` による recovery は決して行わない — activation は durable record に記録する。admit されない inactive split (例: `workspace_id` なし)、または `--no-target-activation` の場合は、`Reason: invalid_args` と strict-rail recovery command を出して fail-closed のままとなる、
    - foreground process は receiver の allowlist に一致しなければならない (`Reason: target_not_agent`): literal `claude` (receiver=`claude`) と literal `codex` (receiver=`codex`) は強い identity。literal `node` と version 付き native binary basename は弱い identity であり、これらは Claude Code と Codex CLI の双方が正当に使う — 弱い場合の cross-binding 保護は Step 9 (window-name binding) と operator discipline が担う。
    すべての check が pass すると、landing marker が観測されたかどうかに関わらず Enter が発行される。outcome は、marker が観測された場合は `sent` / `ok`、観測されなかった場合は `sent` / `queue_enter` である — どちらも実用上の queued submission であって確認済みの landing ではないため、receiver は引き続き durable な Asana task comment / Redmine journal を正本として読む。v0.4 の default 切り替えは preflight を弱めない。scope 外の target (`mozyo-bridge message`、非 agent pane) はこの rail に入らない。
  - `--mode standard` (v0.1 から維持される strict な明示 fallback。`mozyo-bridge message --submit` の default 挙動): marker 観測を厳格に要求する Enter。marker miss は fail-closed である — `C-u` rollback が発行され、Enter は送信されず、outcome は `blocked` / `marker_timeout` となる (sender は receiver の composer がクリアされたことを tmux capture から検証しない)。strict な landing 観測が必要なとき (queue-enter rail 変更後の regression check、queue-pickup 確率が未検証の新規 pane、observability テスト、strict な landing 証跡を要求する audit 要件)、または target が v0.4 default scope の外にあるときに、この rail を明示的に選ぶ。claude receiver への挙動は v0.1 から不変で、default 切り替えによって緩和されない。**codex receiver に限り v0.6 (Redmine #13166) で Enter 後の turn-start 検証が追加された**: marker 観測 + Enter 発行の後、受信 pane の新規出力活動 (turn 開始) を read-only で観測し、観測ありで従来どおり `sent` / `ok`、観測 window 内に活動がなければ `blocked` / `turn_start_unconfirmed` で fail-closed する (`C-u` rollback も自動再送も行わず、marker+body は一度だけ type される。sender は受信 pane を再読して turn 開始を確認してからのみ再送する)。背景の実測と ACK-layer 位置づけは採用 repo の tmux send safety contract (v0.6 節) を正本として参照する。default を override した理由を auditor が replay できるよう、この rail を選んだ理由を durable record に記録する。
- どちらの rail を使う場合でも、durable record (Asana task comment / Redmine journal) が引き続き正本である。pane 通知は pointer である。
- `.agent_handoff/tasks.json` は retired な queue cleanup surface であり、標準の通知 fallback ではない。

## Tool error の解釈

`mozyo-bridge` command (またはその他の tool) が `error: ...` message を出して die したとき、失敗の形を過去の似て見える error に pattern-match する前に、まずその error の literal text を解釈する。literal text が権威ある next-step の source である。過去 session で記憶した「この種の error は通常 fatal / X で回避」という pattern はこれを override しない。

- error が `read target again`、`retry`、`refresh`、`re-run` のような literal な next-action verb、あるいは明示的な `Retry path:` / `Fallback path:` hint を含む場合、より上位の fallback を検討する前に、その verb を verbatim に実行する。その verb が権威ある next step である。
- literal な next-action verb に従った上で新たな失敗が生じた後、または最新の error が next-action verb を全く含まないと明白に確認できた後にのみ、agent はより上位の fallback へ escalate してよい。literal な verb を skip して記憶済みの escape path を優先するのは既知の regression mode である (Asana task 1214779823377861)。
- `mozyo-bridge message --no-submit` と `mozyo-bridge handoff send` の marker-gate 失敗は、retry path と per-preset の `--no-submit` retry budget の両方を示す `hint:` trailer を stderr に出す。これらの trailer は契約の一部である — 読んで従う。飾りとして扱わない。
- `--no-submit` retry budget (上限 3) と標準の `handoff send` retry pool は **別々の budget** である。preset の `Notification fails` branch を発火させてよいか判断する際に、両者の間で試行回数を融通しない。

## 送信側 handoff 境界

`mozyo-bridge` 経由で他の agent に作業を依頼するとき、送信側 agent は handoff 自体のみを検証する:

- target pane が意図した agent と repository context であることを確認する。
- operator message を送信する。
- delivery guard が許すときに message を submit する。agent pane handoff の default は `--mode queue-enter` (v0.4 規範 default) である。`--mode standard` は、strict な landing 観測が必要なとき (regression check、新規 pane、observability テスト、strict-landing の audit 要件)、または target が v0.4 default scope の外 (`mozyo-bridge message`、非 agent pane) のときにのみ使う。
- 任意で、delivery 直後に pane を一度読み、skill の欠落、Notion MCP access の欠落、未 submit の prompt のような明白な blocker を捕捉する。

target pane の polling や監視を標準運用として続けない。handoff の deliver 後は、依頼で指名された durable な正本に依拠する。通常は Redmine journal (`mozyo_bridge` およびその他の Redmine-preset repo)、Asana task comment (Asana-preset repo)、repository の変更、または target agent からの明示的な完了通知である。

operator が target を pane id ではなく自然言語で指名した場合 ("人形使いへ返して", "あっちの Claude に渡して")、送信前に compact な target discovery で解決する。`mozyo-bridge agents targets` は候補を列挙するのみで、選択はしない。一意な候補を `workspace` / `lane` / `role` / `pane_id` / `repo_short` 列に照らして確認し、その上で明示的な `pane_id` へ `--target-repo auto` (または明示的な repo root) を付けて送信する — identity を pane title 単独で決して信用しない。自然名が 0 件または 2 件以上の候補に一致する場合は fail closed とする: 候補を提示するか owner に尋ね、黙って 1 つを選ばない。この解決の利便性は以下のどの境界も緩めない — 特に session 横断の `--to claude` は引き続き拒否され、target session の Codex gateway を経由しなければならない。同じ gateway 規則は 1 つの物理 session 内の lane 境界にも及ぶ: coordinator lane から target lane へ跨る handoff (例えば複数 lane を収容する cockpit session) は、その lane で一意に解決された Claude pane へ直接送るのではなく、**target lane の Codex** pane を経由する。Claude への直接 delivery は同一 lane 内の宛先指定に限る。完全な手順は `references/workflow.md` の `## 自然名 target への handoff` にある。

project の central preset や rule が、ある方向のすべての handoff について sender 通知を義務づけている場合、その要件は task を audit-only、revalidation、doc-only と位置づけることによっても、receiver の事前の pickup 意思表明によっても緩和されない。sender は該当するすべての handoff で通知を試行しなければならない。標準経路の通知を先に試行せずに "un-notified" state を記録するのは送信側の正当化であり、満たされた fallback 条件ではない。

## 結果通知の境界

durable な結果を書くことは handoff にとって必要だが、常に十分とは限らない。作業や audit が他の agent の依頼から始まった場合は、結果を記録した後にその sender へ通知し、durable な正本を読むべきだと分かるようにする。

返信通知は短くし、durable record を指し返すものにする。返信通知を、Redmine journal (`mozyo_bridge` の default)、Asana comment (Asana-preset repo)、repository の変更、その他の指名された正本の代替として使わない。

coordinator / sublane 運用モデル (main の Codex coordinator lane が実装 sublane へ作業を dispatch する) では、この返信境界は常設 rule へ広がる: sublane が handoff-worthy な state — blocked、implementation_done、review_request、review 結果、commit 記録、owner close 承認依頼 — のいずれかに到達したとき、durable anchor を添えた簡潔な callback を coordinator lane の Codex へ送り、coordinator cockpit から作業が stall して見えないようにする。この callback は lane 境界を越えるため、1 つの物理 cockpit session の内側であっても Codex-to-Codex で行い (他 lane の Claude へ直接送らない)、Redmine journal への pointer にとどめ、その copy にしない。完全な手順は `references/workflow.md` の `## Sublane の coordinator callback` にある。

## Release 安全規約

- GitHub Actions OIDC Trusted Publishing を優先する。
- local token upload を production PyPI の標準経路にしない。
- production release の前に CI と TestPyPI install を確認する。
