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
| `stale` | 観測は本物だが freshness bound を超えた | なし |

- **失敗した source は board から落とさない。** 落とすと「到達不能な host」と「Unit が
  無い host」が区別できなくなる。source 行として可視のまま残し、その source だけを
  unactionable にする。
- 1 つでも live な source があれば merged board は `live` である。remote 1 台の不調で
  local の board を止めない。degraded 内訳は `sources` に残る。
- remote 応答は**untrusted input として再検証する**。向こう側が同じ code を動かしている
  ことは検証を省く理由にならない。全 text は client 側で同じ public-safe projection を
  通し、absolute path / credential 形状は `[redacted]` へ畳む。
- 観測 timestamp が無い / parse できない / 未来である場合は `stale` とする。client が
  age を証明できない応答を action authority にしない。

## Remote Unit action

remote Unit は「入力してよい pane」ではない。host 境界越えは governance boundary 越えで
あり、唯一の sanctioned route は**対象環境自身の project gateway** である。

```text
mozyo-bridge herdr unit-board action --unit <unit_id> \
  --issue <id> --journal <id> --kind <kind> --summary <text> [--apply]
```

- 配送は対象 source 上で `project-gateway handoff --to codex --target-repo <root>
  --target-project <name>` を実行する。gateway は project の Codex unit を semantic に
  解決し、`--to claude` を拒否する。client 側は remote pane を名指ししない。
- **preview が既定であり、preview は許可証ではない。** `--apply` は preview が依拠した
  source / Unit / repository identity を再観測し、freshness → identity → 配送の順に検査する。
  いずれかが変わっていれば zero-send で refuse する。
- `--target-repo` に渡す canonical path は対象 host 上の path である。argv 値としてのみ
  存在し、payload / 描画 / journal には出さない。gateway の応答も `result` token だけを
  反映し、その delivery record (pane id / repo root を含む) は生成 host 側に残す。
- local Unit はこの route の対象外である (`local_source_not_routed`)。local には通常の
  same-host handoff command があり、同じことを 2 経路にしない。
- `--summary` は **public-safe projection を素通りする文字列だけを受け付ける**。preview は
  projection を通した文字列を表示するため、projection が書き換える文字列 (control 文字 /
  absolute path / credential 形状 / 正規化される形) を許すと「preview で確認した文字列」と
  「実際に配送する文字列」が食い違う。projection 後と byte 一致しない summary は拒否する。
- refusal は typed reason + 固定文言である。接続値・remote path・exception 本文を出さない。

## 表示 rail の適用範囲

| command | 複数 source | 備考 |
| --- | --- | --- |
| `show` | あり | source が local のみなら pre-#15138 の描画と payload を維持する |
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

- source schema / argv 形状 / 予約 host_id / bound の unit tests
- operator home loader の fail-closed (missing = local-only default / present-but-broken = error)
- remote payload の再検証、cross-source duplicate、freshness、degraded source の unit tests
- 多 source runtime と action rail の injected-runner tests (live host を使わない)
- local-only 不変の regression pin (`tests/regressions/test_issue_15138_local_only_unit_board_preserved.py`)
- local / remote host / remote Dev Container を使った軽量実機確認は #15140 が所有する

## 関連文書

- `vibes/docs/logics/herdr-unit-board.md`
- `vibes/docs/logics/local-remote-cockpit-host-boundary.md`
- `vibes/docs/logics/unit-target-model.md`
- `vibes/docs/logics/herdr-plugin-presentation-consumer-boundary.md`
- `vibes/docs/rules/public-private-boundary.md`
