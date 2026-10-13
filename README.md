# boatrace-data

`boatraceopenapi/api` の公開JSONを取得・検証し、自分のGitHubリポジトリに保存する最小構成です。Python 3.12の標準ライブラリだけを使います。BOATRACE公式サイトを直接スクレイピングする処理はありません。

## 最初に行うこと

1. GitHubで自分の **Public** リポジトリ `boatrace-data` を作成します。
2. このフォルダの中身をリポジトリ直下に登録します。`.github/workflows` も必要です。ZIPそのものを登録しても実行できません。
3. **Actions → Sync JSON → Run workflow** で実行します。日付欄は空欄なら日本時間の当日です。過去日は `2026-10-12` のように指定できます。
4. 緑色の完了表示と `docs/v1/status.json`、日付別JSONを確認します。

この段階では定期取得・Pages公開は無効です。まず手動で動作を確認できます。コードと設定は作成済みですが、GitHub上での実行・公開はまだ検証していません。

手元のGitから登録する場合は、作成直後の空リポジトリに対して、このフォルダで次を実行します。既存リポジトリへ上書きする手順ではありません。

```bash
git init -b main
git add .
git commit -m "Add BOATRACE JSON mirror"
git remote add origin https://github.com/taku-to/boatrace-data.git
git push -u origin main
```

GitHub側の設定によりpushが拒否される場合はActionsの書き込み権限やブランチ保護設定を確認してください。個人用アクセストークンはこのワークフローには不要です。

## Pagesで公開する

1. **Settings → Pages → Build and deployment → Source** を **GitHub Actions** にします。
2. **Settings → Secrets and variables → Actions → Variables** に `ENABLE_PAGES` を作成し、値を `true` にします。SecretではなくRepository variableです。
3. **Sync JSON** をもう一度手動実行します。

アカウント名が `taku-to`、リポジトリ名が `boatrace-data` の場合、公開後は次のURLになります。

| URL末尾（基点 `https://taku-to.github.io/boatrace-data/`） | 内容 |
| --- | --- |
| `v1/2026/20261012.json` | 日付別の元JSON |
| `v1/today.json` | 最終実行時点のJST当日分 |
| `v1/status.json` | 取得日時・成功/失敗・グレード別レース件数 |
| `v1/index.json` | 保存されている日付の一覧 |

ルートには案内ページを置かず、JSONのURLを直接使います。GitHub Pagesは静的配信のため、URLに自由な検索条件を付けて絞り込む処理はありません。WebアプリやWordPress側で必要な日付を取得して絞り込みます。

## 定期取得を有効にする

手動取得に成功したら、同じRepository variablesに `ENABLE_SYNC=true` を追加します。

- 毎時17分：当日分を取得。
- 毎日06:47 JST：当日・前日・前々日を再取得して結果の遅延反映を補完。
- 停止するときは `ENABLE_SYNC=false` にします。手動実行は引き続き可能です。

スケジュールはUTC表記です。GitHub Actionsの実行遅延や上流の更新停止があるため、毎時の最新データ反映を保証するものではありません。最初はこの頻度で稼働を確認し、利用状況に応じて調整します。

`today.json` の日付は利用側でも必ず確認してください。日付変更後、次のジョブまで前日分が残る場合があります。次回実行時に古い日付のエイリアスを削除し、当日分を取得できなければ404となります。日付別の過去データは残ります。

## 保存・エラー時のルール

- 元JSONをそのまま保存します。取得日時や集計は `status.json` に分離します。
- 対応日付は2026年1月1日以降です。過去分の一括取得は初期実装に含みません。
- JSONの形式・対象日・会場番号・レース番号・選手の枠番号を検証します。
- レース前の直前情報・結果の欠損は許容します。
- 同じ日付で既存レースや取得済みの直前情報・結果全体が消えたら更新を止めます。
- 取得・検証に失敗した日付は正常な保存済みJSONを維持し、エラーを記録します。複数日取得は日付ごとに判定します。
- 空の会場一覧は「開催なし」と推測せずエラーにします。正式な取消・訂正による削除も停止対象なので、その場合は上流と保存内容を確認して対応します。
- 内容が変わらないJSONの再書き込みは省きますが、確認時刻の記録のため通常は実行ごとにコミットします。
- エラー時も記録と保存済みデータをPagesに反映してから、ジョブの失敗を報告します。

`status=ok` は取得・構造検証の成功を意味します。公式結果との照合、完全性、上流データの新鮮さを保証するものではありません。`last_success_at` はこの処理の取得成功時刻で、レース情報の更新時刻ではありません。`with_result_count` も結果オブジェクトが存在する件数で、確定保証ではありません。

## グレード

元の `grade_number` を保持し、`status.json` の `summary.grade_race_counts` に次の対応で集計します。未知値や欠損は「不明」とし、一般戦と決めつけません。件数は開催数ではなくレース数です。

| grade_number | 分類 |
| --- | --- |
| 1 | SG |
| 2 | G1 |
| 3 | G2 |
| 4 | G3 |
| 5 | 一般 |
| 6 | PG1 |

女子戦などの分類は追加しません。数値の定義は上流の[Grade enum](https://github.com/turnmark/scraper/blob/main/src/Enums/Grade.php)に基づきます。

## ローカル実行・修正

```bash
python -m unittest discover -s tests -v
python sync.py
python sync.py --date 2026-10-12
python sync.py --recent-days 3
```

`--output` で保存先を変更できます。依存パッケージのインストールは不要です。Linux/macOS向けです。タイムゾーンデータがない環境ではPythonの `tzdata` が必要になる場合があります。

## 初期実装での確認

- 2026-10-12の実際の公開JSONを取得し、14会場・168レースを検証。
- 2026-10-13の実際の公開JSONを取得し、11会場・132レースを検証。取得時点のデータを同梱しています。
- グレード分類、不正JSON、日付不一致、欠落、保存データ保護、日付変更時の失敗を含む8件のテストに合格。
- GitHub上でのコミット・Pages公開・cron実行は、リポジトリ作成後の確認事項です。

## 出典・運用範囲

元データと仕様： [boatraceopenapi/api](https://github.com/boatraceopenapi/api)

このプロジェクトも非公式です。上流のライセンス表示を `UPSTREAM-LICENSE.txt` に同梱しています。元データの追加・修正は上流の提供状況に依存します。

公開リポジトリとGitHubの標準ホステッドランナー、GitHub Pagesを利用し、追加のサーバー契約なしで開始する構成です。データと履歴の増加、Pagesの容量・転送量などの上限に近づいた場合は、保存期間や配信方法を見直します。

参考： [Pagesのカスタムワークフロー](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages) / [Pagesの制限](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits) / [Actionsの課金](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
