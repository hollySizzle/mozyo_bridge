# ADR-0002: 受信側が busy でも Enter を押す (停止は二重送信より害が大きい)

- status: active
- date: 2026-08-16
- related: Redmine #12580 (初出の承認)、#15202 (herdr 側の経緯)、#15537 (実装 US)

## 決定 (規約行)

agent 間送信で受信側の処理開始が確認できないとき、優先すべきは「止めないこと」である。本人確認
(identity / generation / terminal) が成立している同一の受信者に対しては、受信側が busy であっても
bounded な Enter 再送を拒否しない。本文の再入力はしない (exactly-once は不変)。

## 背景

- #12580 (2026-06-26) で owner が承認した設計: 本文は 1 回だけ入力し、Enter だけを 30 秒 window /
  2 秒間隔で押し直す。空の入力欄への Enter は no-op であることが二重送信にならない論拠。
- herdr backend への移行後、#15202 とその 4 往復のレビューで再送は「追加 Enter 最大 1 回・待ち
  15 秒・busy / blocked / unknown は再送拒否」まで狭められた。個々の指摘は安全側として筋が
  通っていたが、owner の優先順位はどの往復でも再確認されなかった。
- 2026-08-16、coordinator → implementer の review 結果通知が同日 4 回、受信側 busy を理由に停止し、
  operator の手動 Enter が事実上のフォールバックになった (#15146 j#106261、#15531 j#106277 /
  j#106285 ほか)。

## 根拠 (逐語引用)

- #12580 j#65384 (owner close approval、2026-06-26): 「queue-enter Enter-only retry is implemented
  and reviewed: marker+body typed once, Enter-only retry, default 30s/2s」を owner が承認して close。
- #15202 j#102578 (owner_intent、2026-08-10): 「止まっているレーンを運用で回し、ガリガリ進める」
- #15202 j#102910 (owner_instruction、2026-08-10): 「まずはこのエンターが遅れないとかも結構
  どうしようもないバグだから、リリースできるまではあなたが…実装してもいい。最悪ではないね」
- 2026-08-16 owner (chat): 「この設計は何回も何回も私がやめろって言って、結局エンターを連打する
  ようにしてたはずなんだけど。…なんかこれで作業が止まる方が二重送信するよりもやばいよねって
  いう意思決定をしたはずなんだけど」

## 影響

- 送信 rail (#15537 で実装): busy を再送拒否の理由にしない。確認 window は受信者の turn 終了に
  連動させてよい。identity / generation / terminal の本人確認 gate、本文 exactly-once は緩めない。
- reviewer: 送信リトライ・配送確認を狭める変更 (再送回数の削減、拒否条件の追加、window の短縮) は
  本 ADR と矛盾する。指摘する場合は本 ADR を名指しし、owner 承認を得ること (ADR-0001)。
- 本 ADR は「無制限の盲目連打」を認めるものではない: 宛先の本人確認が成立しない場合・本文の
  再入力を伴う場合は従来どおり fail-closed が正しい。
