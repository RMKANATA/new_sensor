"""dTmax 演算モジュール (new_sensor 版)

【概要】
- 当日 0:00〜4:00 の間に計測された dT_data の最大値を dTmax として管理します。
- dTmax.csv は「前日」「当日」の最大2行のみ保持します（古い行は自動消去）。
- get_previous_day_dTmax() で前日の dTmax 値を取得できます（翌日の K_data 基準値）。
- update_dTmax() を measure_dT.py から毎分呼び出すことで自動更新します。

【dTmax.csv スキーマ】
  date, sensor_name, dTmax, is_provisional
  2026-08-19, new01, 1.2345, False   ← 前日
  2026-08-20, new01, 0.5678, True    ← 当日
"""

import csv
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR       = Path(__file__).resolve().parent
DTMAX_CSV_PATH = str(BASE_DIR / "dTmax.csv")
SENSOR_NAME    = os.getenv("SENSOR_NAME", "new01")
WINDOW_START   = time(0, 0, 0)   # 集計ウィンドウ開始 (0:00)
WINDOW_END     = time(4, 0, 0)   # 集計ウィンドウ終了 (4:00)


# ==========================================
# 内部ヘルパー
# ==========================================

def _is_in_window(dt: datetime) -> bool:
    """指定日時が 0:00〜4:00 の集計ウィンドウ内かどうかを判定します。"""
    return WINDOW_START <= dt.time() < WINDOW_END


def _read_all_records(filepath: str = DTMAX_CSV_PATH) -> list[dict]:
    """dTmax.csv の全レコードを読み込み、辞書のリストで返します。

    :return: [{"date": str, "sensor_name": str, "dTmax": float, "is_provisional": bool}, ...]
    """
    path = Path(filepath)
    if not path.exists():
        return []

    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            records = []
            for row in reader:
                if not any(v.strip() for v in row.values()):
                    continue
                is_prov_raw = row.get("is_provisional", "False")
                records.append({
                    "date"          : row.get("date", ""),
                    "sensor_name"   : row.get("sensor_name", ""),
                    "dTmax"         : float(row.get("dTmax", 0.0)),
                    "is_provisional": str(is_prov_raw).strip().lower() == "true",
                })
        return records
    except Exception as e:
        print(f"[dTmax] Warning: Failed to read {filepath}: {e}", flush=True)
        return []


def _write_all_records(records: list[dict], filepath: str = DTMAX_CSV_PATH) -> None:
    """dTmax.csv にレコードリストを書き込みます（前日＋当日の最大2行）。"""
    try:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "sensor_name", "dTmax", "is_provisional"])
            for r in records:
                writer.writerow([
                    r["date"],
                    r["sensor_name"],
                    round(r["dTmax"], 6),
                    r["is_provisional"],
                ])
    except Exception as e:
        print(f"[dTmax] Error: Failed to write {filepath}: {e}", flush=True)


def _build_clean_records(
    today_record: dict | None,
    yesterday_record: dict | None,
) -> list[dict]:
    """前日・当日レコードのみのリストを組み立てます。"""
    result = []
    if yesterday_record:
        result.append(yesterday_record)
    if today_record:
        result.append(today_record)
    return result


# ==========================================
# 公開 API
# ==========================================

def get_previous_day_dTmax(filepath: str = DTMAX_CSV_PATH) -> float | None:
    """前日の dTmax 値を取得します（K_data 計算の基準値として使用）。

    dTmax.csv に昨日付けのレコードが存在する場合のみ値を返します。
    存在しない場合は None を返します（measure_dT.py 側で当日値で代替してください）。

    :param filepath: dTmax.csv のパス
    :return: 前日の dTmax 値 (float)、または None
    """
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    for r in _read_all_records(filepath):
        if r["date"] == yesterday_str:
            return r["dTmax"]
    return None


def update_dTmax(dt_data: float, timestamp_dt: datetime | None = None) -> float:
    """dTmax.csv を最新の dTmax 値に更新します。

    - dTmax.csv は「前日」「当日」の最大2行を保持します。
    - 古い行（前日・当日以外）は自動消去されます。
    - 0:00〜4:00 の正規ウィンドウ内: 最大値を更新。
    - ウィンドウ外でも当日データがない場合: 暫定値として初回登録し、より大きい値があれば更新し続けます。

    :param dt_data: 今回計測した dT の値 (℃)
    :param timestamp_dt: 計測日時 (省略時は datetime.now() を使用)
    :return: 現在有効な dTmax 値 (float)
    """
    now           = timestamp_dt or datetime.now()
    today_str     = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    in_window     = _is_in_window(now)

    # 全レコード読み込み → 前日・当日のみ抽出（古い行を自動消去）
    all_records      = _read_all_records()
    today_record     = next((r for r in all_records if r["date"] == today_str), None)
    yesterday_record = next((r for r in all_records if r["date"] == yesterday_str), None)
    # 前日・当日以外が存在した場合はファイルが汚れているので書き直しフラグ
    needs_cleanup    = len(all_records) != (
        (1 if today_record else 0) + (1 if yesterday_record else 0)
    )

    # 当日レコードがない場合（日付変更直後 or 初回起動）
    if today_record is None:
        is_prov = not in_window
        today_record = {
            "date"          : today_str,
            "sensor_name"   : SENSOR_NAME,
            "dTmax"         : dt_data,
            "is_provisional": is_prov,
        }
        _write_all_records(_build_clean_records(today_record, yesterday_record))
        status = "Provisional Initialized" if is_prov else "Initialized"
        print(f"[dTmax] {status}: date={today_str}, dTmax={dt_data:.6f}", flush=True)
        return dt_data

    # 当日レコードがある場合: 更新可否チェック
    can_update    = in_window or today_record.get("is_provisional", False)
    current_dtmax = today_record["dTmax"]

    if can_update and dt_data > current_dtmax:
        is_prov = False if in_window else today_record.get("is_provisional", False)
        today_record = {
            "date"          : today_str,
            "sensor_name"   : SENSOR_NAME,
            "dTmax"         : dt_data,
            "is_provisional": is_prov,
        }
        _write_all_records(_build_clean_records(today_record, yesterday_record))
        status = "Updated" if not is_prov else "Provisional Updated"
        print(f"[dTmax] {status}: {current_dtmax:.6f} -> {dt_data:.6f}", flush=True)
        return dt_data

    # 値の更新はないがクリーンアップが必要な場合のみ書き直す
    if needs_cleanup:
        _write_all_records(_build_clean_records(today_record, yesterday_record))

    return current_dtmax


def dTmax_calc(filepath: str = DTMAX_CSV_PATH) -> float | None:
    """dTmax.csv から当日の dTmax 値を取得して返します。

    :param filepath: dTmax.csv のパス（デフォルト: "dTmax.csv"）
    :return: dTmax 値 (float) または None
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    for r in _read_all_records(filepath):
        if r["date"] == today_str:
            return r["dTmax"]
    return None
