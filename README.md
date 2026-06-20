# テニスコート空き通知

[都立公園スポーツレクリエーション予約システム](https://kouen.sports.metro.tokyo.lg.jp/web/) の
**猿江恩賜公園 / 木場公園**（テニス・人工芝コート）の空き枠を監視し、
新たに空きが出たら **Gmail でメール通知** する。

## 通知条件

- **平日**：19:00〜21:00 の枠のみ
- **土日**：全枠（9:00〜21:00 の6枠）
- 監視範囲：当日から **5週間先** まで
- 「予約あり → 空き」に変わった瞬間だけ通知（同じ枠は重複通知しない）

## 仕組み

`check_availability.py` が予約システムの空き状況API（`rsvWOpeInstSrchVacantAjaxAction.do`）を
セッション経由で叩き、`status==0`（空き）の枠を抽出。前回結果（`state.json`）との差分のみメール送信する。

GitHub Actions の cron で **10分ごと**（JST 7:00〜25:00）に自動実行。`state.json` はキャッシュで引き継ぐ。

## セットアップ（GitHub Actions）

1. このリポジトリを GitHub に push。
2. Gmail の **アプリパスワード** を発行（2段階認証が必要）：
   <https://myaccount.google.com/apppasswords>
3. リポジトリの **Settings → Secrets and variables → Actions** で以下を登録：

   | Secret | 値 |
   |---|---|
   | `GMAIL_USER` | 送信元 Gmail アドレス |
   | `GMAIL_APP_PASSWORD` | 発行したアプリパスワード（16桁） |
   | `NOTIFY_TO` | 通知先メールアドレス（カンマ区切りで複数可） |

4. **Actions** タブから「テニスコート空きチェック」を一度 *Run workflow* で手動実行して動作確認。

## ローカル実行

```bash
pip install requests
export GMAIL_USER=you@gmail.com
export GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
export NOTIFY_TO=you@gmail.com
python check_availability.py
```

認証情報を設定しない場合はメールを送らず、内容を標準出力に表示する（動作確認用）。

## 設定変更

`check_availability.py` の冒頭：

- `PARKS` … 監視する公園（公園コード`bcd`・施設コード`icd`）
- `EVENING_FROM` … 平日に通知する開始時刻（既定 1900）
- `WEEKS_AHEAD` … 何週間先まで見るか（環境変数でも指定可）

## 注意

- 空き枠の閲覧はログイン不要だが、**予約には会員ログインが必要**（このツールは通知まで）。
- 予約システムには無断キャンセル等への**ペナルティ制度**があるため、予約は手動で慎重に。
- 過度な高頻度アクセスは避ける（10分間隔程度が妥当）。
