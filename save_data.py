"""センサデータ CSV 保存スクリプト (new_sensor 版)

【概要】
- temp.csv から最新の測定データを読み込み、日次 CSV ファイルに追記保存します。
- POST 処理とは独立して実行・呼び出し可能です。

【保存先】
  data/{sensor_name}_{YYYY-MM-DD}.csv
  例: data/new01_2026-08-19.csv

【保存カラム】
  timestamp, hostname, sensor_name, v1, ch2, ch3, dT, dTmax, samples
"""

import csv
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TEMP_CSV_PATH = str(BASE_DIR / "temp.csv")  # 読み込む CSV ファイルパス
DATA_DIR      = str(BASE_DIR / "data")      # 日次データ保存ディレクトリ


def read_latest_csv(filepath: str = TEMP_CSV_PATH) -> dict | None:
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


def save_data_csv(row: dict, data_dir: str = DATA_DIR) -> bool:
    """計測データを日次 CSV ファイルに追記保存します。

    保存先: {data_dir}/{sensor_name}_{YYYY-MM-DD}.csv

    :param row: temp.csv の行辞書
    :param data_dir: 保存先ディレクトリ名
    :return: 保存成功時 True、失敗時 False
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

    timestamp   = row.get("timestamp", "")
    parts       = timestamp.split(" ", 1)
    measure_date = parts[0] if len(parts) >= 1 and parts[0] else datetime.now().strftime("%Y-%m-%d")
    sensor_name = row.get("sensor_name", "unknown")
    hostname    = row.get("hostname", "")

    dir_path  = Path(data_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    file_path = dir_path / f"{sensor_name}_{measure_date}.csv"

    write_header = not file_path.exists()

    try:
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

        print(f"[OK] Saved to '{file_path}'", flush=True)
        return True

    except Exception as e:
        print(f"[Error] Failed to save data CSV: {e}", flush=True)
        return False


if __name__ == "__main__":
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] Starting data save...", flush=True)

    row = read_latest_csv()
    if row is None:
        sys.exit(1)

    success = save_data_csv(row)
    sys.exit(0 if success else 1)
