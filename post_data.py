"""センサデータ POST スクリプト (new_sensor 定期実行サービス版)

【概要】
- temp.csv から最新の測定データを読み込み、指定 API エンドポイントに JSON 形式で POST します。
- 毎分 20 秒のタイミングで 1 分ごとに自動送信します（measure_dT.py の 00 秒測定完了を待って送信）。
- 計測データは POST の成否にかかわらず日次 CSV (data/配下) に無条件保存されます。
- 送信失敗時は SQLite キュー (data/pending.db) に安全に蓄積され、通信回復時に自動再送されます。
- 【重要】サーバー側で重複排除を行わない仕様に対応し、ディスク永続化 (post_state.json) を
  用いて同一タイムスタンプの重複送信をクライアント側で厳格に防止します。
- cron @reboot や systemd 等で常駐サービスとして実行することを想定しています。
"""

import json
import logging
import os
from pathlib import Path
import socket
import sys
import time
from datetime import datetime, timedelta

# --- 1. カレントディレクトリ移動 & sys.path 追加 ---
BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# --- 2. 標準ライブラリ ---
import csv

# --- 3. サードパーティライブラリ ---
import requests
from dotenv import load_dotenv
load_dotenv()

# --- 4. ローカルモジュール ---
from common import (
    sd_notify,
    seconds_until_next_slot,
    setup_logger,
    update_status,
)
from save_data import save_data_csv
from send_queue import SendQueue

# ==========================================
# 設定定数
# ==========================================
API_URL         = os.getenv("API_URL", "https://api.rmkanata.com/data")
SENSOR_NAME     = os.getenv("SENSOR_NAME", "new01")
CSV_PATH        = BASE_DIR / "temp.csv"

# 再送制御パラメータ
POST_RESEND_MAX        = int(os.getenv("POST_RESEND_MAX", "30"))         # 1サイクルあたりの最大再送件数
POST_RESEND_BUDGET_SEC = float(os.getenv("POST_RESEND_BUDGET_SEC", "30.0"))  # 1サイクルの再送時間予算（秒）
STALE_WARN_MIN         = float(os.getenv("STALE_WARN_MIN", "3.0"))       # temp.csv が何分古ければ警告するか

logger = setup_logger("new_sensor.post", log_file="post.log")


# ==========================================
# HTTP セッション管理
# ==========================================
_session: requests.Session | None = None


def get_http_session() -> requests.Session:
    """コネクションプーリングを有効にした requests.Session を返します。"""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; new_sensor/2.0)",
        })
    return _session


def read_latest_csv(filepath: Path | str | None = None) -> dict | None:
    """temp.csv の最新データ行を読み込み、辞書形式で返します。"""
    target_path = Path(filepath) if filepath is not None else Path(CSV_PATH)
    if not target_path.exists():
        logger.warning(f"CSV file not found: {target_path}")
        return None

    try:
        with open(target_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = [row for row in reader if any(v.strip() for v in row.values())]

        if not rows:
            logger.warning(f"No data rows found in {target_path}")
            return None

        return rows[-1]

    except Exception as e:
        logger.error(f"Failed to read CSV {target_path}: {e}")
        return None



def build_payload(row: dict) -> dict:
    """CSV 行データから POST 用 JSON ペイロードを組み立てます。"""
    def to_float(value: str, default: float = 0.0) -> float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    hostname  = row.get("hostname", socket.gethostname())
    timestamp = str(row.get("timestamp", "")).strip()

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


def execute_post(payload: dict, endpoint: str = API_URL, timeout: tuple = (10, 20)) -> tuple[bool, int | None, str | None]:
    """指定エンドポイントに JSON データを POST します。

    :param payload: 送信データ辞書
    :param endpoint: POST 先 URL
    :param timeout: (connect_timeout, read_timeout)
    :return: (is_success, http_status_code, error_message)
    """
    session = get_http_session()
    try:
        response = session.post(url=endpoint, json=payload, timeout=timeout)
        if response.status_code in (200, 201, 204):
            return True, response.status_code, None
        else:
            err_msg = f"HTTP {response.status_code}: {response.text[:200]}"
            return False, response.status_code, err_msg

    except requests.exceptions.ConnectionError as e:
        return False, None, f"ConnectionError: {e}"
    except requests.exceptions.Timeout as e:
        return False, None, f"Timeout: {e}"
    except requests.exceptions.RequestException as e:
        return False, None, f"RequestException: {e}"
    except Exception as e:
        return False, None, f"UnexpectedError: {e}"


def check_data_staleness(timestamp_str: str) -> None:
    """temp.csv のタイムスタンプが古い場合に警告を出力します。"""
    if not timestamp_str:
        return
    try:
        data_dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        age_seconds = (datetime.now() - data_dt).total_seconds()
        age_minutes = age_seconds / 60.0
        if age_minutes >= STALE_WARN_MIN:
            logger.warning(
                f"[STALE] temp.csv timestamp '{timestamp_str}' is {age_minutes:.1f} minutes old. "
                f"measure_dT.py may not be running or ADC is unresponsive."
            )
    except Exception:
        pass


def post_cycle(queue: SendQueue, csv_path: Path | str | None = None) -> dict:
    """1回分のデータ送信・保存サイクルを実行します。

    1. temp.csv から最新データを読み込む
    2. 新規データがあれば、まず日次 CSV に無条件保存（欠測防止）
    3. 既に送信済みタイムスタンプの場合は送信スキップ（重複送信の絶対防止）
    4. 最新データを POST 送信
       - 成功: 送信済みタイムスタンプを永続化、キュー内の過去データを再送
       - 失敗: キューに蓄積
    """
    cycle_result = {
        "status": "idle",
        "timestamp": None,
        "posted": False,
        "error": None,
        "http_status": None,
    }

    row = read_latest_csv(csv_path)
    if row is None:
        logger.warning("[Warn] Skipping cycle: No valid data in temp.csv")
        cycle_result["error"] = "No valid data in temp.csv"
        return cycle_result


    current_timestamp = str(row.get("timestamp", "")).strip()
    cycle_result["timestamp"] = current_timestamp

    # stale 判定
    check_data_staleness(current_timestamp)

    # 1. ローカル日次CSVへの無条件保存（POST成否に関係なく保存）
    save_data_csv(row)

    # 2. 重複チェック（既に送信済みのタイムスタンプなら送信スキップ）
    if queue.is_already_posted(current_timestamp):
        logger.info(f"Skipping POST: Data for timestamp '{current_timestamp}' was already sent.")
        cycle_result["status"] = "already_posted"
        return cycle_result

    # 3. 最新データのペイロード作成と POST
    payload = build_payload(row)
    logger.info(f"Posting measurement data (timestamp: {current_timestamp})...")
    logger.debug(f"Payload: {json.dumps(payload, ensure_ascii=False)}")

    success, status_code, err_msg = execute_post(payload)
    cycle_result["http_status"] = status_code
    cycle_result["error"] = err_msg

    if success:
        logger.info(f"[OK] POST succeeded | status={status_code} | timestamp={current_timestamp}")
        queue.mark_as_posted(current_timestamp)
        cycle_result["posted"] = True
        cycle_result["status"] = "success"

        # 4. キューに溜まっている未送信データの再送処理
        resend_pending_data(queue)

    else:
        logger.error(f"[Error] POST failed | status={status_code} | error={err_msg}")
        # 未送信キューに登録
        queue.enqueue(current_timestamp, payload)
        cycle_result["status"] = "failed"

    return cycle_result


def resend_pending_data(queue: SendQueue) -> None:
    """キューに蓄積された過去の未送信データを古い順に再送します。"""
    pending_items = queue.fetch_pending(limit=POST_RESEND_MAX)
    if not pending_items:
        return

    logger.info(f"[Resend] Starting backlog resend for {len(pending_items)} items...")
    start_time = time.time()
    sent_count = 0

    for row_id, ts, payload in pending_items:
        # 時間予算チェック
        if time.time() - start_time > POST_RESEND_BUDGET_SEC:
            logger.warning(f"[Resend] Time budget ({POST_RESEND_BUDGET_SEC}s) reached. Postponing remaining items.")
            break

        # 再送実行
        success, status_code, err_msg = execute_post(payload)
        if success:
            logger.info(f"[Resend OK] Resent timestamp={ts} | status={status_code}")
            queue.remove(ts)
            sent_count += 1
        else:
            logger.warning(f"[Resend Failed] timestamp={ts} | status={status_code} | error={err_msg}. Aborting resend.")
            queue.record_attempt_failure(ts, err_msg or "Unknown error")
            break  # 1つ失敗したら通信状態が悪いと判断し、残りは次回に回す

    logger.info(f"[Resend] Completed. Successfully resent {sent_count} items. Remaining queue: {queue.get_pending_count()}")


# ==========================================
# メイン処理 (1分周期・20秒実行ループ)
# ==========================================
def main_loop():
    logger.info("Starting new_sensor post service...")
    logger.info(f"  Endpoint    : {API_URL}")
    logger.info(f"  Sensor Name : {SENSOR_NAME}")
    logger.info(f"  CSV path    : {CSV_PATH}")

    if not API_URL:
        logger.error("API_URL is not set in .env")
        sys.exit(1)

    # 送信キューおよび送信済み状態マネージャ初期化
    queue = SendQueue()

    # systemd に準備完了を通知
    sd_notify("READY=1")

    consecutive_failures = 0
    last_post_ok_at: str | None = None

    status_data = {
        "service": "post",
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_post_ok_at": None,
        "last_http_status": None,
        "consecutive_failures": 0,
        "pending_count": queue.get_pending_count(),
        "status": "running",
    }
    update_status("status_post.json", status_data)

    while True:
        try:
            # 毎分 20 秒に合わせて待機 (offset=20, period=60)
            wait_sec = seconds_until_next_slot(offset_sec=20.0, period_sec=60.0, min_gap=0.5)
            logger.debug(f"Sleeping {wait_sec:.1f}s until next post slot (:20)...")
            time.sleep(wait_sec)

            # 送信サイクルを実行
            result = post_cycle(queue)

            if result["posted"]:
                consecutive_failures = 0
                last_post_ok_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            elif result["status"] == "failed":
                consecutive_failures += 1
                logger.warning(f"[Failure Streak] Consecutive post failures: {consecutive_failures}")

            # 状態ファイル更新
            status_data.update({
                "last_post_ok_at": last_post_ok_at,
                "last_http_status": result.get("http_status"),
                "last_error": result.get("error"),
                "consecutive_failures": consecutive_failures,
                "pending_count": queue.get_pending_count(),
                "status": "warning" if consecutive_failures > 0 else "running",
            })
            update_status("status_post.json", status_data)

            # systemd 管理下であればループ生存を通知
            sd_notify("WATCHDOG=1")

        except KeyboardInterrupt:
            logger.info("Post service stopped by user.")
            break
        except Exception as e:
            # cron @reboot 等で予期せぬ例外が出ても絶対にプロセスを落とさない
            logger.error(f"[Main Loop Error] Unexpected exception in post loop: {e}", exc_info=True)
            time.sleep(5)


if __name__ == "__main__":
    main_loop()
