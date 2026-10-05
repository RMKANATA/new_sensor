# new_sensor 運用・デプロイ手順書

本システムは、ラズパイ上の ADC（ADS1115）データ読み取りと、リモート API へのデータ送信を行うサービスです。

---

## 運用形態の選択

運用の形態として以下の **2通り** のどちらでも利用可能です。
遠隔地運用の安定性としては **Option A (cron監視併用)** または **Option B (systemd推奨)** を選択してください。

---

### Option A: 現在の cron @reboot 運用を継続する場合（おすすめ）

既存の `@reboot` 設定を活かしつつ、万が一のプロセス停止に備えて死活監視スクリプト（`cron_watchdog.sh`）を定期実行します。

#### 1. crontab の設定
`crontab -e` を開き、以下のように設定します。

```cron
# 起動時の自動実行
@reboot cd /home/plant01/new_sensor && nohup python3 -u measure_dT.py >> logs/measure_stdout.log 2>&1 &
@reboot cd /home/plant01/new_sensor && nohup python3 -u post_data.py >> logs/post_stdout.log 2>&1 &

# 5分ごとの死活監視（プロセスが停止していれば自動再起動）
*/5 * * * * /home/plant01/new_sensor/deploy/cron_watchdog.sh >> /home/plant01/new_sensor/logs/watchdog.log 2>&1
```

※ パスや Python コマンド（仮想環境等の場合）は環境に合わせて調整してください。

---

### Option B: systemd サービスへ移行する場合

OS 標準のサービス監視、自動再起動、ウォッチドッグ機能を利用したい場合はこちらを選択します。

#### 1. サービスの配置と有効化
```bash
# 既存の crontab 設定（@reboot 等）をコメントアウトまたは削除
crontab -e

# ユニットファイルのコピー
sudo cp deploy/new_sensor-measure.service /etc/systemd/system/
sudo cp deploy/new_sensor-post.service /etc/systemd/system/

# パスやユーザー名の確認・編集（必要に応じて）
# sudo nano /etc/systemd/system/new_sensor-measure.service
# sudo nano /etc/systemd/system/new_sensor-post.service

# 反映と起動
sudo systemctl daemon-reload
sudo systemctl enable --now new_sensor-measure.service
sudo systemctl enable --now new_sensor-post.service
```

#### 2. 状態・ログの確認
```bash
sudo systemctl status new_sensor-measure.service
sudo systemctl status new_sensor-post.service

# ログの確認
journalctl -u new_sensor-measure.service -f
journalctl -u new_sensor-post.service -f
```

---

## 状態確認とトラブルシューティング

プログラムの稼働状況や直前の状態は、以下のファイルで即座に確認できます。

### 1. 状態ファイル (JSON)
```bash
# 測定プロセスの状態（ADC連続エラー数、時計ジャンプ有無、最終サンプル時刻等）
cat status_measure.json

# 送信プロセスの状態（最終送信日時、HTTPステータス、未送信キュー残件数等）
cat status_post.json
```

### 2. ローテーションログ
`logs/` ディレクトリ配下に、各 2MB（最大5世代）で安全にログが保持されます。
- `logs/measure.log`: ADC測定、1分要約、ハードウェア回復ログ
- `logs/post.log`: POST送信結果、再送キューの状況、通信エラー詳細
