"""ADS1115 アナログ温度センサ自動測定スクリプト (new_sensor 版)

【概要】
- ADS1115 (ADC) を経由して 5秒周期でセンサ値を取得し、1分ごとに平均化します。
- ch1: Vref 測定 (分圧抵抗 R1, R2)
- ch2: 加熱側サーミスタ温度 (T2 / Hs)
- ch3: 参照側サーミスタ温度 (T3 / Rs)
- 1分ごとに dT (T2 - T3 のトリム平均) を算出し、dTmax.py で当日の dTmax を更新します。
- 測定結果は常に最新の1行として 'temp.csv' に上書き保存されます。
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
import math
import socket
import time
from datetime import datetime, timedelta

# --- 3. サードパーティライブラリ ---
from dotenv import load_dotenv

# --- 4. ローカルモジュール ---
from dTmax import update_dTmax, get_previous_day_dTmax

load_dotenv()

# ==========================================
# 設定定数
# ==========================================
CSV_PATH    = "temp.csv"
SENSOR_NAME = os.getenv("SENSOR_NAME", "new01")
HOSTNAME    = socket.gethostname()

# 抵抗定数
R1 = float(os.getenv("R1", "1000.0"))
R2 = float(os.getenv("R2", "1000.0"))
R3 = float(os.getenv("R3", "100000.0"))
R4 = float(os.getenv("R4", "100000.0"))

# NTC サーミスタ定数
NTC_R0 = float(os.getenv("NTC_R0", "10000.0"))
NTC_T0 = float(os.getenv("NTC_T0", "298.15"))  # 25℃ = 298.15K
NTC_B  = float(os.getenv("NTC_B", "3380.0"))

# ==========================================
# ADS1115 ハードウェア初期化（モックフォールバック付き）
# ==========================================
_IS_MOCK = False

try:
    import board
    import busio
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn

    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS.ADS1115(i2c, address=0x48)
    ads.gain = 2
    ads.data_rate = 16

    ch1_in = AnalogIn(ads, 0)
    ch2_in = AnalogIn(ads, 1)
    ch3_in = AnalogIn(ads, 2)
    ch4_in = AnalogIn(ads, 3)
    print("[Init] ADS1115 hardware initialized successfully.", flush=True)

except Exception as e:
    _IS_MOCK = True
    print(f"[Init] ADS1115 hardware not detected ({e}). Using mock sensor readings.", flush=True)


def read_raw_voltages() -> tuple[float, float, float]:
    """ADCから ch1, ch2, ch3 の生電圧(V)を取得します。

    :return: (v1, v2, v3)
    """
    if _IS_MOCK:
        # モック（ダミー）データ生成
        # v1: ~1.65V (Vref=3.3V), v2: ~0.8V (~25℃), v3: ~0.78V (~24℃)
        import random
        v1 = 1.65 + random.uniform(-0.01, 0.01)
        v2 = 0.80 + random.uniform(-0.005, 0.005)
        v3 = 0.78 + random.uniform(-0.005, 0.005)
        return v1, v2, v3

    return ch1_in.voltage, ch2_in.voltage, ch3_in.voltage


# ==========================================
# 計算ヘルパー
# ==========================================

def calc_vref(v: float) -> float:
    """分圧された電圧から Vref (基準電圧) を逆算します。"""
    return v * (R1 + R2) / R2


def calc_temperature(vout: float, vref: float, r_fixed: float) -> tuple[float | None, float | None]:
    """分圧回路の出力電圧からサーミスタ温度 (℃) と抵抗値 (Ω) を算出します。

    :param vout: 出力電圧
    :param vref: 基準電源電圧
    :param r_fixed: 固定抵抗値
    :return: (温度℃, サーミスタ抵抗値) または (None, None)
    """
    if vout <= 0 or vout >= vref:
        return None, None

    r_ntc = r_fixed * vout / (vref - vout)

    try:
        temp_k = 1.0 / (
            1.0 / NTC_T0 +
            1.0 / NTC_B *
            math.log(r_ntc / NTC_R0)
        )
        return temp_k - 273.15, r_ntc
    except (ValueError, ZeroDivisionError):
        return None, None


def trim_average(data: list[float]) -> float:
    """最大値と最小値を除いたトリム平均を算出します。"""
    if len(data) == 0:
        return 0.0
    if len(data) <= 2:
        return sum(data) / len(data)
    s = sorted(data)
    return sum(s[1:-1]) / (len(s) - 2)


def save_temp_csv(
    timestamp_str: str,
    hostname: str,
    sensor_name: str,
    voltage: float,
    hs_data: float,
    rs_data: float,
    dt_data: float,
    dtmax: float,
    k_data: float,
    samples: int,
    filepath: str = CSV_PATH,
) -> None:
    """測定結果を temp.csv に上書き保存します。"""
    try:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "hostname", "sensor_name",
                "voltage", "HS_data", "RS_data", "dT_data", "dTmax", "K_data", "samples",
            ])
            writer.writerow([
                timestamp_str,
                hostname,
                sensor_name,
                round(voltage, 3),
                round(hs_data, 3),
                round(rs_data, 3),
                round(dt_data, 3),
                round(dtmax, 3),
                round(k_data, 4),
                samples,
            ])
    except Exception as e:
        print(f"[Error] CSV write failed: {e}", flush=True)


# ==========================================
# メインサンプリング・集計ループ
# ==========================================
if __name__ == "__main__":
    print(f"Starting ADS1115 Temperature Monitor ({SENSOR_NAME})...", flush=True)

    # 5秒境界に合わせてサンプリング開始
    next_sample = (int(time.time()) // 5 + 1) * 5
    current_minute = datetime.now().minute
    current_date   = datetime.now().date()

    # 前日 dTmax をロード（K_data 計算の基準値。なければ当日値で代替）
    prev_dTmax: float | None = get_previous_day_dTmax()
    prev_label = f"{prev_dTmax:.6f}" if prev_dTmax is not None else "N/A (fallback: today's dTmax)"
    print(f"  Previous day dTmax : {prev_label}", flush=True)

    vref_list: list[float] = []
    t2_list: list[float]   = []
    t3_list: list[float]   = []
    dt_list: list[float]   = []

    try:
        while True:
            # 正確な5秒周期待機
            delay = next_sample - time.time()
            if delay > 0:
                time.sleep(delay)
            next_sample += 5

            # 電圧取得
            try:
                v1, v2, v3 = read_raw_voltages()
                vref = calc_vref(v1)
                t2, _ = calc_temperature(v2, vref, R3)
                t3, _ = calc_temperature(v3, vref, R4)

                if t2 is not None and t3 is not None:
                    dt = t2 - t3
                    vref_list.append(vref)
                    t2_list.append(t2)
                    t3_list.append(t3)
                    dt_list.append(dt)

                    time_str = datetime.now().strftime("%H:%M:%S")
                    print(
                        f"[{time_str}] Vref={vref:.3f}V | T2(Hs)={t2:.3f}C | T3(Rs)={t3:.3f}C | dT={dt:.3f}C",
                        flush=True,
                    )
            except Exception as e:
                print(f"[Warn] Read sample error: {e}", flush=True)

            # 分切り替え判定 (毎分 00 秒)
            now = datetime.now()
            if now.minute != current_minute:
                # 日付変更検知 → prev_dTmax を前日値に更新
                if now.date() != current_date:
                    prev_dTmax = get_previous_day_dTmax()
                    current_date = now.date()
                    prev_label = f"{prev_dTmax:.6f}" if prev_dTmax is not None else "N/A (fallback: today's dTmax)"
                    print(f"[New Day] prev_dTmax updated: {prev_label}", flush=True)

                if len(dt_list) > 0:
                    vref_avg = sum(vref_list) / len(vref_list)
                    t2_avg   = sum(t2_list) / len(t2_list)
                    t3_avg   = sum(t3_list) / len(t3_list)
                    dt_avg   = trim_average(dt_list)
                    samples  = len(dt_list)

                    # dTmax の更新（当日分 → 翌日の prev_dTmax として使われる）
                    dtmax_val = update_dTmax(dt_avg, now)

                    # K_data: 前日 dTmax を基準（なければ当日 dTmax で代替）
                    ref_dtmax = prev_dTmax if prev_dTmax is not None else dtmax_val
                    k_data = round((ref_dtmax - dt_avg) / dt_avg, 4) if dt_avg != 0.0 else 0.0

                    # タイムスタンプ (00秒固定)
                    timestamp_00 = now.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:00")

                    # temp.csv 上書き保存
                    save_temp_csv(
                        timestamp_str=timestamp_00,
                        hostname=HOSTNAME,
                        sensor_name=SENSOR_NAME,
                        voltage=vref_avg,
                        hs_data=t2_avg,
                        rs_data=t3_avg,
                        dt_data=dt_avg,
                        dtmax=dtmax_val,
                        k_data=k_data,
                        samples=samples,
                    )

                    # JSON出力
                    summary = {
                        "timestamp": timestamp_00,
                        "hostname": HOSTNAME,
                        "sensor_name": SENSOR_NAME,
                        "voltage": round(vref_avg, 3),
                        "HS_data": round(t2_avg, 3),
                        "RS_data": round(t3_avg, 3),
                        "dT_data": round(dt_avg, 3),
                        "dTmax":   round(dtmax_val, 3),
                        "K_data":  k_data,
                        "samples": samples,
                    }
                    print(f"\n[Minute Summary] {json.dumps(summary, ensure_ascii=False)}\n", flush=True)

                # バッファクリア
                vref_list.clear()
                t2_list.clear()
                t3_list.clear()
                dt_list.clear()

                current_minute = now.minute

    except KeyboardInterrupt:
        print("\nMeasurement stopped by user.", flush=True)
