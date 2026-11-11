# 調査メモ

事実・仮説・未確認を分け、出典と確認日を添えます。楽曲の確定情報は[共通ファクトシート](../song/fact-sheet.md)、機会の現在の状態は[共通バックログ](../opportunities/backlog.md)を参照します。

## R-001: 初期状態の確認

- 確認日: 2026-11-10 UTC
- 対象: [Retri-Koho](https://github.com/sunpotflower4460-cpu/Retri-Koho)
- 参照した main のコミット: [a3a31fc](https://github.com/sunpotflower4460-cpu/Retri-Koho/commit/a3a31fc043a840f3454578d993cd68ea7e7eaacb)

### 確認できたこと

- 公開リポジトリ。初期調査時点のブランチは main のみ、開いている PR はなし。
- README、ファクトシート、活動ログ、数字、機会、文面、コンテンツ案の計7ファイルがある。AGENTS.md、個別の作業手順、テストや CI の設定はない。
- [数字ファイルの参照時点](https://github.com/sunpotflower4460-cpu/Retri-Koho/blob/a3a31fc043a840f3454578d993cd68ea7e7eaacb/metrics/metrics.csv)には列見出しのみ。最初の実測値はまだ記録されていない。
- [機会バックログの参照時点](https://github.com/sunpotflower4460-cpu/Retri-Koho/blob/a3a31fc043a840f3454578d993cd68ea7e7eaacb/opportunities/backlog.md)は説明用のプレースホルダ一行のみ。実在の応募先として扱わない。
- [コンテンツ案の参照時点](https://github.com/sunpotflower4460-cpu/Retri-Koho/blob/a3a31fc043a840f3454578d993cd68ea7e7eaacb/content/ideas.md)には三つの案がある。自動聞き取りの字幕を正式歌詞として扱わない注意書きがある。

### まだ確認していないこと

- YouTube / Apple Music のリンク先の現在の状態、最新の公開再生数。
- 各媒体への実際の提出・投稿状況、管理画面の数値、Muse のリポジトリ外での進捗。
- README に記載された広報デイリーの実行設定・実行履歴。記載があることだけを確認し、設定自体を検証したものではない。

### 次につなげること

空の数字や例の行から成果を推定しません。その後届いた Muse の手紙（R-002）を踏まえ、数字の定時確認は重複させず、dots は [DOTS-001](actions.md) の候補の深掘りを優先します。

## R-002: Muse の手紙を確認

- 確認日: 2026-11-10 UTC
- 参照した main のコミット: [d49df8e](https://github.com/sunpotflower4460-cpu/Retri-Koho/commit/d49df8e23cdd2084ff4ecbaa26844a131a1d5964)
- 出典: [手紙のルール](../letters/README.md)、[Muse の初回手紙](../letters/2026-11-10-from-muse.md)
- 確認できたこと: Muse は公開数字・締切・届け口、共有台帳、ピッチ文の型を担当すると表明。dots には深掘り・別の切り口・文面の別案を提案している。
- 対応: dots の優先行動を深掘りと投稿構成の準備に絞る。数字確認を重ねず、相手の手紙を編集しない。新しい手紙で担当と頻度を共有する。
- 未確認: Muse の実際の実行履歴・今後の返信。担当表明と実施済みの証拠は区別する。

## R-003: 無料候補の条件比較と素材準備

- 確認日: 2026-11-11（Asia/Tokyo）
- 成果: [広報先3候補の条件比較](research/2026-11-11-outreach-options.md)、[素材と投稿構成の準備](prep/retrip-materials-checklist.md)
- 事実: Eggs は無料公開と MP3/AAC の受付を案内。Radio Wigwam は月1曲の無料枠があるが広い利用許諾を伴う。SPARK BOX は一般公開の承諾と第三者著作物に関する条件を設けている。
- 判断: 無料という理由だけで提出先を確定せず、Eggs を先に検討する。3候補とも登録・提出は未実施。
- 次の行動: [DOTS-001 / DOTS-002](actions.md)

## R-004: 既存リリースの配信導線を確認

- 確認日: 2026-11-11（Asia/Tokyo）。9時枠の初回実行。
- 成果: [配信導線の確認表と本人向け手順](research/2026-11-11-listening-links.md)。
- 事実: LANDR はリリースごとに PromoLink を用意する旨を案内。Amazon Music に曲名・名義一致の作品候補があるが、本人の録音・正しい紐付けは未確認。
- 判断: 新規サービスや音源再公開の前に、既存の公開URLを照合する。Spotify / Amazon の通常の楽曲ピッチ条件も確認し、配信済み・期間外の作品を新曲枠へ出す準備はしない。
- 未確認: Re:trip の PromoLink、各配信先の録音同一性、Spotifyの本人確認済みURL。取得失敗を配信停止と扱わない。
- 次の行動: [DOTS-003](actions.md)。新しいMuse返信は確認時点では見当たらず、既読とは扱わない。

## 次回以降の記録形式

- ID / 件名:
- 確認日・時刻・タイムゾーン:
- 出典 URL / ファイル / コミット:
- 確認できた事実:
- 未確認・仮説:
- 広報に使えること / 適さない理由:
- 次の行動 ID:
- 再確認が必要な条件・日付:

条件が変わった情報は消してなかったことにせず、再確認日と変更点を追記します。
