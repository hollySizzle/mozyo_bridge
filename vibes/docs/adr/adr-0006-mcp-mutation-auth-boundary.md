# ADR-0006: 変更操作の caller 認証は runtime perimeter に置き、偽造不能認証は共有機能と同時に導入する

- status: active
- date: 2026-08-17
- related: Redmine #15152 j#106959 (owner 決定の逐語記録)、#15152 review j#106903 (finding_clientenvspoof)、#15579 (偽造不能認証の導入 US)、#14546 (external-client coordinator proxy)、#15195 (runtime 発行 receipt、upstream NO-GO)

## 決定 (規約行)

MCP / CLI の変更操作 (lane actuation 等) の caller 認証を、in-process の ambient identity
(呼び出し元プロセスの環境変数) で行わない。現段階の信頼境界は runtime perimeter
(同一ホスト・attested pane 内に居ること) であると正直に宣言し、偽造不能な caller 認証
(operator トークン → 証明書 / #15195) は user 貸し借り・ネットワーク露出機能と**同時に**導入する。

## 背景

#15152 は変更操作を MCP tool として提供する US で、R1〜R3 のレビューで「呼び出し元が正当な
coordinator か」を副作用前に検証する仕組みを段階的に厚くしてきた。R3 で tmux backend の
`LiveSublaneActuatorOps` に env-based の送信者チェック (`MOZYO_WORKSPACE_ID` /
`MOZYO_AGENT_ROLE` / `MOZYO_LANE_ID` を repo anchor・coordinator binding・default lane と照合)
を追加したところ、R3 review j#106903 の finding_clientenvspoof が「stdio MCP server は外部
client が spawn するプロセスであり、その環境変数は client 制御下。照合先の値は repo/board/
journal marker から半公開なので、client が一致値を自己設定すれば偽造できる」と指摘した。

事実調査 (実装者):
- `.mozyo-bridge/workspace-anchor.json` は gitignore で clone に付属しないが、その `workspace_id`
  は unit board 表示・`mzb1_<id>_...` assigned name・journal marker (`workspace=<id>`) に現れ半公開。
- coordinator provider は committed の role binding か既定 `codex`、lane_id は定数 `default`。
- したがって現行チェックは「対象 workspace の 32 桁 ID を知っているか」を問うだけで、同一ホストでは
  無意味、remote でも半公開 ID の知識のみ。「平文認証の第一歩」ですらない。

`external-client-coordinator-proxy.md` (#14546) は既に「caller env は authority ではない。
fallback としても読まない。手動 `MOZYO_*` export = identity 偽造」と規定しており、正本と実装が
矛盾していた。より深い構造として、in-process 実行 + ambient identity は「効果が呼び出し元の
プロセス内で起きる」ため外部 client に対して原理的に安全化できない (proxy rail は効果を attested
runtime の live pane で起こすため forge が無力になる、という対比)。

偽造不能な認証の候補 — runtime 発行の generation-bound receipt — は #15195 が upstream NO-GO と
判定済み (Herdr Discussions #2652 待ち)。今の道具では強い認証を作れない。この状況で「認証して
いる」と主張し続けること自体が、実利と無関係に defect である。

## 根拠 (逐語引用)

出所: #15152 j#106959 (owner 決定の逐語記録、2026-08-17 chat)。

- 「これを自分でやる分には、自分のサーバーで自分のパソコンでやる分には全く問題ないんだけれど、
  これを他の相手と接続する。つまり…CEO の育てたエージェント…に対して…そいつの環境を容易に
  破壊できてしまうっていうのが問題になるね。」
- 「偽造されるっていうのは結構問題があるな。もちろん最近の LLM だったらかなり倫理感が高いから、
  あんまりそういう攻撃とかはやらないはずなんだけど。とはいえ、あった方がいいなって思った。」
- 「最終的には認証書き形式 (=証明書) にするべきだと思うけど、一旦は平文でもいいかなって思うわ。
  というのもまだそんなユーザー解放機能とか貸し借り機能っていうのはないから。」
- (実装者の落とし所「実態を正直に書く + 情報漏れ修正 + 認証は将来 issue へ委譲」と ADR 起草の
  承認要求に対し)「よし、いいよ。やっていい。承認する。どちらもはい。」

## 影響

- **正直性が第一義**: 変更操作の tool 説明・SERVER_INSTRUCTIONS・設計文書は、持っていない caller
  認証を「durable authority を検証する」と主張してはならない。信頼境界 (runtime perimeter =
  同一ホスト・attested pane) を明示する。env-based チェックは弱いまま残してよいが「偽造防止
  authority」と表現しない。
- **偽造不能認証は起動条件付き**: operator スコープの共有トークン (repo/board/marker に出ない値の
  提示) を弱いが本物の第一歩とし、最終的に caller が自己生成できない capability (herdr の
  peer-credential 本人確認 API、または #15195 の runtime 発行 receipt) へ置換する。導入の起動条件は
  **user 貸し借り / ネットワーク越し MCP 露出機能を作るとき**であり、それまでは #15579 に park する。
- **#15152 finding の扱い**: finding_clientenvspoof は本 ADR の threat model 明示化により
  deferred (現デプロイの model 外、再評価トリガー = 露出拡大機能の実装)。finding_reasonproseleak は
  in-model の実漏れであり deferred にせず修正する。
- **将来の露出拡大は本 ADR に必ず引っかかる**: 「攻撃者がいないから検証を消す」のではなく「信頼
  境界を runtime に置くと明示する」書き方であるため、貸し借り / ネットワーク露出を設計する時点で
  境界前提の変更として顕在化する。その変更は本 ADR を supersede する owner 裁定を要する。
- 本 ADR と矛盾する「ambient env を caller authority として扱う」実装、および「caller 認証を
  していると主張しつつ ambient env 照合で済ませる」変更は `adr_conflict_gate` の対象。
