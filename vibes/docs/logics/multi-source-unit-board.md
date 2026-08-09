# Multi-source Unit board

Redmine #15138。ローカルクライアントが、local / remote SSH host / その host 上の
Dev Container など**複数の Herdr サーバー**から public-safe な Unit 情報を取得して
1 画面へ集約表示し、選択した remote Unit への操作を対象環境の coordinator gateway
経由で実行するための実装正本。

`herdr-unit-board.md` (#15114) が単一 Herdr サーバーの識別表示を定義する。本 doc は
その client 側の集約層だけを追加し、identity / authority / preview-first の境界は
一切緩めない。

## 結論

**server state は統合しない。統合するのは表示だけである。**

`local-remote-cockpit-host-boundary.md` (#11817) の結論は維持する。socket / DB /
workspace state / pane id は host ごとに閉じたままで、mozyo-bridge が提供するのは
host-aware な観測の投影と、host 境界を越える安全な gateway route である。

```text
local Herdr server      -> in-process observation      ┐
remote host Herdr server-> `mozyo-bridge herdr unit-board show --json` 経由 ├-> 1 つの merged board
Dev Container Herdr     -> 同上 (container exec)       ┘
```

各 source は**自分自身の board を投影する**。remote host が自分の workspace registry、
workflow role binding、lane metadata を解決するため、client は他 host の registry を
読まないし、cross-host registry も同期しない。

remote への問い合わせは **単一サーバー投影に固定する**。`show --local-only` は設定済み
source を無視してその host 自身の server だけを答える flag であり、client は remote 呼び出しで
必ず付ける。付けないと、自分の source を持つ host は **その host の merged board** を返し、
client が要求していない server の行が混入する。相互登録では再帰 fan-out にもなる。
producer 側 flag だけに依存せず、**consumer 側も `sources` envelope を持つ応答を
`reload_required` として拒否する**（旧版 remote や flag 忘れでも fail-closed になる）。

## Source 設定 (operator-scoped)

正本は operator home の `unit-board-sources.yaml` である。repo には置かない。これは
規約ではなく機構である: ssh 宛先と container 名はこの file にしか存在せず、checkout に
同行せず、diff にも public document にも現れない。

```yaml
version: 1
sources:
  - host_id: devbox           # 短い lowercase slug。identity join key
    kind: ssh
    ssh_target: <operator の ssh 宛先>
    label: dev host           # board が表示してよい唯一の operator 値
  - host_id: devcontainer
    kind: container
    container: <container 名>
    via: devbox               # 1 hop のみ。container 経由の container は拒否
```

- `local` source は常に存在する。宣言しなくても暗黙に先頭へ入る。
- `host_id: local` は local source の予約語。remote が名乗ると local Unit の key 空間に
  衝突するため拒否する。local source を別名にしたい場合は `label` を使う。
- **`label` は source 間で一意でなければならない**。label は board が表示する唯一の source
  identity なので、重複すると 2 つの server の行が画面上で区別できなくなる。identity 層では
  なく表示層を経由して同じ混同に到達する経路であり、config で閉じる。一意性は **public-safe
  projection を通した値**で判定する（board が描画するのは projection 後の値であり、raw が違っても
  Unicode 正規化・空白圧縮で同一表示になりうる）。
- `kind` ごとに許可 key を固定し、未知 key・misplaced key・重複 key・version 不一致は
  fail-closed。`yaml.safe_load` の duplicate key は宣言順で解決せず拒否する。
- 接続値 (`ssh_target` / `container` / `mozyo_binary`) は argv 構築の入力にすぎない。
  payload、描画行、refusal detail のいずれにも出さない。
- source 数と接続 timeout は bound する (`MAX_SOURCES` / `MAX_CONNECT_TIMEOUT_SECONDS`)。
  1 refresh あたり source ごとに 1 接続が発生するため。

### argv 形状 (arbitrary remote shell を作らない)

source は 3 つの固定 argv 形状のいずれかに解決する。shell へ渡す文字列は ssh の
remote command 1 箇所だけで、そこは token ごとに `shlex.quote` する。operator 値が
第 2 command へ広がる経路を作らない。

| kind | argv |
| --- | --- |
| `local` | in-process 観測 (subprocess を作らない) |
| `ssh` | `ssh -o BatchMode=yes -o ConnectTimeout=<n> -T -- <target> '<quoted command>'` |
| `container` | `<docker\|podman> exec <container> mozyo-bridge ...` |
| `container` + `via: <ssh>` | 上記 exec argv を ssh remote command として 1 hop nest |

`BatchMode=yes` は必須である。到達不能・未認証の host が password prompt で block すると
board 全体が固まるため、可視の `unavailable` へ速やかに degrade させる。

## Identity

Unit identity は `host_id + workspace_id + lane_id` である。

- **local-only の payload 形状も byte 一致を維持する。** source envelope を持たない snapshot の
  row payload は pre-#15138 の key 集合そのままで、host 修飾 (`host_id` / `host_label` /
  `duplicate_scope`) は merged projection でのみ付く。
- **local Unit の opaque key は #15114 当時と byte 一致を維持する。** key は Herdr display
  metadata (`mozyo_unit`) へ書かれるため、key がずれると local server の全 managed pane の
  label が黙って変わる。host-qualified key は domain separator 付きで hash するため、
  remote source が local key 空間の値を作ることはできない。
- **remote Unit の client 側 key は、remote 自身の opaque key から導出する。** 表示用に
  bound された `workspace_id` / `lane_id` から key を作り直すと、切り詰められた prefix を
  identity にしてしまう (`herdr-unit-board.md` の「表示用に切り詰めた identity を action
  入力へ戻さない」規則)。
- **同一 `(workspace_id, lane_id)` が複数 source に存在するのは正常である。** local と
  remote に同じ repo が checkout されていれば必ず起きる。これらは別 Unit として残し、
  `duplicate_scope: cross_source` で可視化する。`host_id` で区別できるので操作は禁止しない。
- action 入力に使う `workspace_id` は canonical registry 形状 (32 hex) を明示的に検査する。
  形状が違えば「表示値かもしれない」ため action 入力は存在しないものとして扱う。

## Source state と fail-closed

| state | 意味 | action authority |
| --- | --- | --- |
| `live` | 今この source を読めた | あり |
| `unavailable` | 到達不能 / 非 0 exit / spawn 失敗 / timeout | なし |
| `reload_required` | 応答が読めない (schema 不一致・重複 key・bound 超過) | なし |
| `stale` | 観測は本物だが freshness bound を超えた（client 側 round trip、または remote 応答自身の観測時刻） | なし |

- **失敗した source は board から落とさない。** 落とすと「到達不能な host」と「Unit が
  無い host」が区別できなくなる。source 行として可視のまま残し、その source だけを
  unactionable にする。
- 1 つでも live な source があれば merged board は `live` である。remote 1 台の不調で
  local の board を止めない。degraded 内訳は `sources` に残る。
- remote 応答は**untrusted input として再検証する**。向こう側が同じ code を動かしている
  ことは検証を省く理由にならない。全 text は client 側で同じ public-safe projection を
  通し、absolute path / credential 形状は `[redacted]` へ畳む。
- freshness は **2 つの独立した dimension の連言**である。両方が成り立って初めて action
  authority になる。**未来時刻の扱いは dimension ごとに異なり、以下がその唯一の記述である**
  （全面適用の一般則として読まない）。
  - **client 側 round trip** — 応答が「いつ届いたか」。**単一の時計**で測るため skew は原理的に
    生じない。したがって負の age（未来）は skew ではなく矛盾であり、**未来は一律 `stale`**。
    bound は短い。欠落・parse 不能も `stale`。
  - **remote 応答自身の `observed_at`** — 応答が「いつ観測したと主張しているか」。**2 台の時計の
    比較**であり、小さな前倒しは通常の skew である。したがって **明示 bound
    (`MAX_REMOTE_CLOCK_SKEW_SECONDS`) 内の未来は `live`、それを超えれば `stale`**。過去側の bound
    (`MAX_REMOTE_PAYLOAD_AGE_SECONDS`) は client 側より緩く取る。厳しくすると軽微な skew を持つ
    host が恒久的に unactionable になり、fail-closed ではあるが役に立たない。欠落・parse 不能は
    `stale`。
  - 境界 parser は **clock を必須引数として受け取る**。省略できる形にすると「undated な応答が
    live になる」呼び出し方が残り、trust boundary の fail-open そのものになる。
- **remote 応答の identity invariant は client 側で再計算する。** 文字列の再 projection だけでは
  不十分で、遠隔が自己申告する `identity_state` をそのまま採用してはならない。local producer が
  `ambiguous` にする矛盾（同一 Unit 内の provider 重複）は client 側でも検出し、declared 値と
  連言する。degrade は local producer と同じ粒度（当該 row のみ）とし、board の残りは使えるまま
  にする。
- **remote registry の `canonical_path` は argv 投入前に厳格検証する**（絶対 path・制御文字なし・
  長さ bound）。他 host の registry は untrusted input であり、その値が argv 要素になる。
  subprocess 境界に到達してから例外で落ちると、固定 reason の refusal 契約を破る。
- **action 対象は `authority_state=resolved` の Unit に限る**。これは「対象 repo の durable な
  workflow role binding を遠隔 host が読めた」ことの表明であり、それが無い Unit は表示に留める。

## Remote Unit action

remote Unit は「入力してよい pane」ではない。host 境界越えは governance boundary 越えで
あり、唯一の sanctioned route は**対象環境自身の project gateway** である。

```text
mozyo-bridge herdr unit-board action --unit <unit_id> \
  --target-project <adopted project scope> \
  --issue <id> --journal <id> --kind <kind> --summary <text> [--apply]
```

- 配送は対象 source 上で `project-gateway handoff --to codex --target-repo <root>
  --target-project <scope>` を実行する。gateway は project の Codex unit を semantic に
  解決し、`--to claude` を拒否する。client 側は remote pane を名指ししない。
- **`--target-repo` は registry の canonical path（= Git worktree root = workspace authority）から
  解決してよい。`--target-project` は解決しない。** registry の `project_name` は display
  metadata かつ dir 名 default であり、role / scope authority ではない（正本:
  `workflow-step-command-design.md` の「registry `project_name` を role/scope authority に
  しない」）。board の `project_label` も public-safe projection を通った表示値である。
  したがって adopted project scope は **operator が明示宣言する**入力とし、client は合成しない。
  未指定は zero-send。
- **preview が既定であり、preview は許可証ではない。** `--apply` は preview が依拠した
  source / Unit / repository identity を再観測し、freshness → identity → 配送の順に検査する。
  いずれかが変わっていれば zero-send で refuse する。
- `--target-repo` に渡す canonical path は対象 host 上の path である。argv 値としてのみ
  存在し、payload / 描画 / journal には出さない。gateway の応答も `result` token だけを
  反映し、その delivery record (pane id / repo root を含む) は生成 host 側に残す。
- local Unit はこの route の対象外である (`local_source_not_routed`)。local には通常の
  same-host handoff command があり、同じことを 2 経路にしない。
- **operator が入力する自由文（`--summary` / `--target-project`）が、設定済みの接続値
  （`ssh_target` / `container` / 既定でない `mozyo_binary`）を含む場合は拒否する**。preview
  payload は貼り付け可能な出力面であり、そこに接続値が載れば source file を非 tracked に
  置いた意味が失われる。対象 source だけでなく **全 source** の値を対象にする。**refusal 自身も
  出力面**なので、refused preview は operator の自由文を反映せず durable anchor と kind だけを持つ。
  検出は **token 境界**（前後が英数字でない位置）で行い、**値の長さで例外を作らない**。契約は
  「接続値を公開面へ出さない」であり、短い値だけ契約外にする根拠は無い（`restart db now` は
  container `db` の開示、`dbus` は非開示）。3 文字以上の値は語中への埋め込みも検出する。
- **配送成否は exit code で判定しない。** 対象 gateway の **構造化 outcome** を読み、共有 authority
  (`injection_stage_for_outcome`) が `submitted_confirmed` を返した場合のみ `delivered` とする。
  exit 0 でも composer に置いただけの `pending_input` や marker 未観測の `queue_enter` は未配送で
  あり、正本は `delivery_outcome_gate` / `injection_stage`（同一の問いに複数箇所が別答を出した
  #14232 の経緯を持つ）。status / reason token を局所で再検査せず、**共有 authority を評価する**。
  欠落・非 JSON・非 object・authority が位置づけられない outcome はすべて typed `delivery_failed`
  とし、remote stdout の値を detail へ反射しない。
- `--summary` は **public-safe projection を素通りする文字列だけを受け付ける**。preview は
  projection を通した文字列を表示するため、projection が書き換える文字列 (control 文字 /
  absolute path / credential 形状 / 正規化される形) を許すと「preview で確認した文字列」と
  「実際に配送する文字列」が食い違う。projection 後と byte 一致しない summary は拒否する。
- refusal は typed reason + 固定文言である。接続値・remote path・exception 本文を出さない。

## 表示 rail の適用範囲

| command | 複数 source | 備考 |
| --- | --- | --- |
| `show` | あり | source が local のみなら pre-#15138 の描画と payload を維持する。`--local-only` は設定を無視して単一サーバー投影を返す（client の集約要求用） |
| `watch` | あり | remote 設定時は refresh cadence に下限を課す (接続 storm 防止) |
| `sources` | あり | 診断専用。全 source が live でなければ非 0 |
| `action` | remote のみ | 上記 |
| `sync` | local のみ | 他 server の pane metadata を client から書かない (下記) |
| `interact` | local のみ | pane geometry 操作は local Herdr server の live pane に対するもの |

**metadata sync は意図的に local 限定である。** Herdr の pane metadata 書き込みは他 server の
live pane の mutation であり、その host の plugin が既にそこで実行している。client からも
書くと、共通の lock を持たない 2 番目の writer になる。client と remote server の関係は
read-only observation + 1 本の routed な preview-first action に限る。

## 検証

- source schema / argv 形状 / 予約 host_id / label 一意性 / 接続値開示判定 / bound の unit tests
- operator home loader の fail-closed (missing = local-only default / present-but-broken = error)
- remote payload の再検証（text の再 projection + identity invariant の再計算）、nested envelope 拒否、
  cross-source duplicate、2 dimension の freshness（欠落 / parse 不能 / 過去・未来の境界）、
  degraded source（到達不能 / 読めない応答の分離）、remote registry path の厳格検証の unit tests
- 配送判定が共有 injection-stage authority を評価していること（exit 0 の非配送 outcome を
  `delivered` にしない）の unit tests
- 多 source runtime と action rail の injected-runner tests (live host を使わない)
- local-only 不変の regression pin (`tests/regressions/test_issue_15138_local_only_unit_board_preserved.py`)
- local / remote host / remote Dev Container を使った軽量実機確認は #15140 が所有する

## 関連文書

- `vibes/docs/logics/herdr-unit-board.md`
- `vibes/docs/logics/local-remote-cockpit-host-boundary.md`
- `vibes/docs/logics/unit-target-model.md`
- `vibes/docs/logics/herdr-plugin-presentation-consumer-boundary.md`
- `vibes/docs/rules/public-private-boundary.md`
