"""センサデータ POST スクリプト (new_sensor 定期実行サービス版)

【概要】
- temp.csv から最新の測定データを読み込み、指定 API エンドポイントに JSON 形式で POST します。
- 毎分 20 秒のタイミングで 1 分ごとに自動送信します（measure_dT.py の 00 秒測定完了を待って送信）。
- 送信成功時に日次 CSV (data/配下) にも追記保存します。
- 前回送信時と同じタイムスタンプのデータは送信をスキップし、重複送信を防ぎます。
- cron の @reboot や systemd 等で常駐サービスとして実行することを想定しています。

【送信先エンドポイント】
  https://api.rmkanata.com/data (デフォルト / .env の API_URL で変更可能)

【POST JSON スキーマ】
  {
      "hostname": "raspberrypi",
      "timestamp": "2026-08-19 13:20:00",
      "sensors": {
          "new": {
              "voltage": 3.3,
              "HS_data": 25.123,
              "RS_data": 24.567,
              "dT_data": 0.556,
              "K_data": 0.123
          }
      }
  }
"""

import os
import sys
from pathlib import Path

# --- 1. カレントディレクトリ移動 & sys.path 追加（最優先で実行） ---
BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# --- 2. 標準ライブラリ ---
import csv
import json
import socket
import time
from datetime import datetime, timedelta

# --- 3. サードパーティライブラリ ---
import requests
from dotenv import load_dotenv

# --- 4. ローカルモジュール ---
from save_data import save_data_csv

load_dotenv()

# ==========================================
# 設定定数
# ==========================================
API_URL     = os.getenv("API_URL", "https://api.rmkanata.com/data")
SENSOR_NAME = os.getenv("SENSOR_NAME", "new01")
CSV_PATH    = "temp.csv"

# ==========================================
# グローバル状態管理
# ==========================================
_last_posted_timestamp: str | None = None  # 前回送信成功したタイムスタンプ


def read_latest_csv(filepath: str = CSV_PATH) -> dict | None:
    """temp.csv の最新データ行を読み込み、辞書形式で返します。

    :param filepath: 読み込む CSV ファイルパス
    :return: カラム名をキーとした辞書、または None
    """
    path = Path(filepath)
    if not path.exists():
        print(f"[Warn] CSV file not found: {filepath}", flush=True)
        return None

    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = [row for row in reader if any(v.strip() for v in row.values())]

        if not rows:
            print(f"[Warn] No data rows found in {filepath}", flush=True)
            return None

        return rows[-1]

    except Exception as e:
        print(f"[Error] Failed to read CSV: {e}", flush=True)
        return None


def build_payload(row: dict) -> dict:
    """CSV 行データから POST 用 JSON ペイロードを組み立てます。

    :param row: temp.csv の行辞書
    :return: POST ペイロード辞書
    """
    def to_float(value: str, default: float = 0.0) -> float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    hostname  = row.get("hostname", socket.gethostname())
    timestamp = row.get("timestamp", "")

    return {
        "hostname": hostname,
        "timestamp": timestamp,
        "sensors": {
            "new": {
                "voltage": round(to_float(row.get("voltage")), 3),
                "HS_data": round(to_float(row.get("HS_data")), 3),
                "RS_data": round(to_float(row.get("RS_data")), 3),
                "dT_data": round(to_float(row.get("dT_data")), 3),
                "K_data":  round(to_float(row.get("K_data")), 4),
            }
        },
    }


def post_data(payload: dict, endpoint: str = API_URL) -> bool:
    """指定されたエンドポイントに JSON データを POST します。

    :param payload: 送信するデータ辞書
    :param endpoint: POST 先 URL
    :return: 成功時 True、失敗時 False
    """
    try:
        response = requests.post(
            url=endpoint,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=10,
        )
        response.raise_for_status()
        print(
            f"[OK] POST succeeded | status={response.status_code} | endpoint={endpoint}",
            flush=True,
        )
        return True

    except requests.exceptions.ConnectionError:
        print(f"[Error] Connection failed: {endpoint}", flush=True)
    except requests.exceptions.Timeout:
        print(f"[Error] Request timed out: {endpoint}", flush=True)
    except requests.exceptions.HTTPError as e:
        print(f"[Error] HTTP error: {e}", flush=True)
    except requests.exceptions.RequestException as e:
        print(f"[Error] Request failed: {e}", flush=True)

    return False


def post_cycle() -> bool:
    """1回分のデータ送信・保存サイクルを実行します。

    1. temp.csv から最新データを読み込む
    2. 前回送信時と同じタイムスタンプの場合はスキップする
    3. ペイロードを組み立てる
    4. API に POST する
    5. 日次 CSV に保存する

    :return: 成功時 True、スキップまたは失敗時 False
    """
    global _last_posted_timestamp

    row = read_latest_csv()
    if row is None:
        print("[Warn] Skipping cycle: No valid data in temp.csv", flush=True)
        return False

    current_timestamp = row.get("timestamp", "")

    # 前回送信データと同一（更新されていない）の場合はスキップ
    if current_timestamp and current_timestamp == _last_posted_timestamp:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[{now_str}] Skipping cycle: Data already sent (timestamp: {current_timestamp})",
            flush=True,
        )
        return False

    payload = build_payload(row)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] Posting measurement data (timestamp: {current_timestamp})...", flush=True)
    print(f"       Payload: {json.dumps(payload, ensure_ascii=False)}", flush=True)

    # POST 送信
    success = post_data(payload)

    if success:
        # 送信成功時のみタイムスタンプを記録（通信失敗時は次回リトライ可能にする）
        _last_posted_timestamp = current_timestamp
        # 日次 CSV に保存
        save_data_csv(row)

    return success


# ==========================================
# メイン処理 (1分周期・20秒実行ループ)
# ==========================================
if __name__ == "__main__":
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] Starting new_sensor post service...", flush=True)
    print(f"  Endpoint    : {API_URL}", flush=True)
    print(f"  Sensor Name : {SENSOR_NAME}", flush=True)
    print(f"  CSV path    : {CSV_PATH}", flush=True)

    if not API_URL:
        print("[Error] API_URL is not set in .env", flush=True)
        sys.exit(1)

    # --- 初回待機処理（直近の「20秒」に合わせて開始） ---
    now = datetime.now()
    if now.second < 20:
        target_time = now.replace(second=20, microsecond=0)
    else:
        target_time = (now + timedelta(minutes=1)).replace(second=20, microsecond=0)

    initial_sleep = (target_time - now).total_seconds()
    print(
        f"Waiting {initial_sleep:.1f}s until first post at {target_time.strftime('%H:%M:%S')}...",
        flush=True,
    )
    if initial_sleep > 0:
        time.sleep(initial_sleep)

    try:
        while True:
            # 送信サイクルを実行
            post_cycle()

            # --- 次回送信時刻 (次の分 20秒) の計算と待機 ---
            next_target_time = target_time + timedelta(minutes=1)
            sleep_sec = (next_target_time - datetime.now()).total_seconds()

            if sleep_sec > 0:
                time.sleep(sleep_sec)

            target_time = next_target_time

    except KeyboardInterrupt:
        print("\nPost service stopped by user.", flush=True)
