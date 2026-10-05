"""センサデータ CSV 保存スクリプト (new_sensor 版)

【概要】
- temp.csv または渡された行データから日次 CSV ファイルに追記保存します。
- POST 処理とは独立して実行・呼び出し可能です。
- 重複行の追記を防止するチェック機能を含みます。

【保存先】
  data/{sensor_name}_{YYYY-MM-DD}.csv
  例: data/new01_2026-08-19.csv

【保存カラム】
  timestamp, hostname, sensor_name, voltage, HS_data, RS_data, dT_data, dTmax, K_data, samples
"""

import csv
import logging
import os
from pathlib import Path
import sys
from datetime import datetime

from common import setup_logger

BASE_DIR = Path(__file__).resolve().parent
TEMP_CSV_PATH = str(BASE_DIR / "temp.csv")  # 読み込む CSV ファイルパス
DATA_DIR      = str(BASE_DIR / "data")      # 日次データ保存ディレクトリ

logger = setup_logger("new_sensor.save_data", log_file="save_data.log")


def read_latest_csv(filepath: str = TEMP_CSV_PATH) -> dict | None:
    """temp.csv の最新データ行を読み込み、辞書形式で返します。

    :param filepath: 読み込む CSV ファイルパス
    :return: カラム名をキーとした辞書、または None
    """
    path = Path(filepath)
    if not path.exists():
        logger.warning(f"CSV file not found: {filepath}")
        return None

    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = [row for row in reader if any(v.strip() for v in row.values())]

        if not rows:
            logger.warning(f"No data rows found in {filepath}")
            return None

        return rows[-1]

    except Exception as e:
        logger.error(f"Failed to read CSV: {e}")
        return None


def get_last_csv_timestamp(file_path: Path) -> str | None:
    """日次CSVファイルの最終行のタイムスタンプを高速に取得します。"""
    if not file_path.exists():
        return None
    try:
        with open(file_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = [row for row in reader if row and any(cell.strip() for cell in row)]
            if len(rows) >= 2:  # ヘッダ + 1行以上
                return rows[-1][0].strip()
    except Exception as e:
        logger.warning(f"Could not read last timestamp from {file_path}: {e}")
    return None


def save_data_csv(row: dict, data_dir: str = DATA_DIR) -> bool:
    """計測データを日次 CSV ファイルに追記保存します。
    既にファイル内の最終行と同じタイムスタンプである場合は追記せずスキップします。

    保存先: {data_dir}/{sensor_name}_{YYYY-MM-DD}.csv

    :param row: temp.csv の行辞書
    :param data_dir: 保存先ディレクトリ名
    :return: 保存成功時または重複スキップ時 True、異常失敗時 False
    """
    def to_float(value: str, default: float = 0.0) -> float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    def to_int(value: str, default: int = 0) -> int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    timestamp   = str(row.get("timestamp", "")).strip()
    if not timestamp:
        logger.warning("save_data_csv: row does not have valid timestamp")
        return False

    parts       = timestamp.split(" ", 1)
    measure_date = parts[0] if len(parts) >= 1 and parts[0] else datetime.now().strftime("%Y-%m-%d")
    sensor_name = row.get("sensor_name", "unknown")
    hostname    = row.get("hostname", "")

    try:
        dir_path = Path(data_dir)
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{sensor_name}_{measure_date}.csv"

        write_header = not file_path.exists()

        # 重複チェック：既存ファイルの最終行が同じタイムスタンプなら追記しない
        last_ts = get_last_csv_timestamp(file_path)
        if last_ts and last_ts == timestamp:
            logger.debug(f"Data for timestamp '{timestamp}' already exists in '{file_path}'. Skipping append.")
            return True

        with open(file_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow([
                    "timestamp",
                    "hostname",
                    "sensor_name",
                    "voltage",
                    "HS_data",
                    "RS_data",
                    "dT_data",
                    "dTmax",
                    "K_data",
                    "samples",
                ])
            writer.writerow([
                timestamp,
                hostname,
                sensor_name,
                round(to_float(row.get("voltage")), 3),
                round(to_float(row.get("HS_data")), 3),
                round(to_float(row.get("RS_data")), 3),
                round(to_float(row.get("dT_data")), 3),
                round(to_float(row.get("dTmax")), 3),
                round(to_float(row.get("K_data")), 4),
                to_int(row.get("samples")),
            ])

        logger.info(f"[OK] Saved to '{file_path}' (timestamp: {timestamp})")
        return True

    except Exception as e:
        logger.error(f"[Error] Failed to save data CSV: {e}")
        return False


if __name__ == "__main__":
    logger.info("Starting data save...")

    row = read_latest_csv()
    if row is None:
        sys.exit(1)

    success = save_data_csv(row)
    sys.exit(0 if success else 1)
