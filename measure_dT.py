"""ADS1115 アナログ温度センサ自動測定スクリプト (new_sensor 版)

【概要】
- ADS1115 (ADC) を経由して 5秒周期でセンサ値を取得し、1分ごとに平均化します。
- ch1: Vref 測定 (分圧抵抗 R1, R2)
- ch2: 加熱側サーミスタ温度 (T2 / Hs)
- ch3: 参照側サーミスタ温度 (T3 / Rs)
- 1分ごとに dT (T2 - T3 のトリム平均) を算出し、dTmax.py で当日の dTmax を更新します。
- 測定結果はアトミックに 'temp.csv' に上書き保存されます。
- cron @reboot や systemd サービスでの長期常駐運用に対応。
  - ADC ハング対策（SIGALRM によるタイムアウト監視）
  - I2C 通信エラー時の自動再初期化および回復待機ループ（プロセスを落とさない）
  - アトミック書き込みによるファイル破損防止
  - 壁時計ジャンプ耐性
"""

import csv
import json
import math
import os
from pathlib import Path
import signal
import socket
import sys
import time
from datetime import datetime, timedelta

# --- 1. カレントディレクトリ移動 & sys.path 追加 ---
BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv()

# --- 2. ローカルモジュール ---
from common import (
    atomic_write_text,
    sd_notify,
    seconds_until_next_slot,
    setup_logger,
    update_status,
)
from dTmax import get_previous_day_dTmax, update_dTmax

# ==========================================
# 設定定数
# ==========================================
CSV_PATH        = BASE_DIR / "temp.csv"
SENSOR_NAME     = os.getenv("SENSOR_NAME", "new01")
HOSTNAME        = socket.gethostname()
ALLOW_MOCK      = os.getenv("ALLOW_MOCK", "0").strip() == "1"

# 抵抗定数
R1 = float(os.getenv("R1", "1000.0"))
R2 = float(os.getenv("R2", "1000.0"))
R3 = float(os.getenv("R3", "100000.0"))
R4 = float(os.getenv("R4", "100000.0"))

# NTC サーミスタ定数
NTC_R0 = float(os.getenv("NTC_R0", "10000.0"))
NTC_T0 = float(os.getenv("NTC_T0", "298.15"))  # 25℃ = 298.15K
NTC_B  = float(os.getenv("NTC_B", "3380.0"))

# ADC 監視・回復パラメータ
ADC_READ_TIMEOUT_SEC = float(os.getenv("ADC_READ_TIMEOUT_SEC", "3.0"))  # 1回の読み取り上限
ADC_REINIT_AFTER     = int(os.getenv("ADC_REINIT_AFTER", "12"))         # 連続エラーで再初期化 (約1分)

logger = setup_logger("new_sensor.measure", log_file="measure.log")


# ==========================================
# ADC 読み取りタイムアウト (SIGALRM)
# ==========================================
class ADCReadTimeout(Exception):
    """ADCの読み取りが設定時間を超過してハングした場合の例外"""
    pass


def _alarm_handler(signum, frame):
    raise ADCReadTimeout(f"ADC read timed out ({ADC_READ_TIMEOUT_SEC}s)")


# SIGALRM ハンドラ設定（UNIX環境）
try:
    signal.signal(signal.SIGALRM, _alarm_handler)
except (AttributeError, ValueError):
    logger.warning("SIGALRM is not supported in this environment.")


# ==========================================
# ハードウェア制御 (I2C / ADS1115)
# ==========================================
_i2c = None
_ads = None
_ch1_in = None
_ch2_in = None
_ch3_in = None
_is_mock_active = False


def init_adc() -> bool:
    """I2C および ADS1115 ハードウェアを初期化します。"""
    global _i2c, _ads, _ch1_in, _ch2_in, _ch3_in, _is_mock_active

    # 既存リソースのクリーンアップ
    _i2c = None
    _ads = None
    _ch1_in = None
    _ch2_in = None
    _ch3_in = None

    try:
        import board
        import busio
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn

        _i2c = busio.I2C(board.SCL, board.SDA)
        _ads = ADS.ADS1115(_i2c, address=0x48)
        _ads.gain = 2
        _ads.data_rate = 16

        _ch1_in = AnalogIn(_ads, 0)
        _ch2_in = AnalogIn(_ads, 1)
        _ch3_in = AnalogIn(_ads, 2)

        _is_mock_active = False
        logger.info("[Init] ADS1115 hardware initialized successfully.")
        return True

    except Exception as e:
        if ALLOW_MOCK:
            _is_mock_active = True
            logger.warning(f"[Init] Hardware not detected ({e}). ALLOW_MOCK=1 is set: using mock readings.")
            return True
        else:
            _is_mock_active = False
            logger.error(f"[Init] Failed to initialize ADS1115 hardware: {e} (ALLOW_MOCK is False)")
            return False


def read_raw_voltages() -> tuple[float, float, float]:
    """ADCから ch1, ch2, ch3 の生電圧(V)を取得します。タイムアウト監視付き。

    :return: (v1, v2, v3)
    """
    if _is_mock_active:
        import random
        v1 = 1.65 + random.uniform(-0.01, 0.01)
        v2 = 0.80 + random.uniform(-0.005, 0.005)
        v3 = 0.78 + random.uniform(-0.005, 0.005)
        return v1, v2, v3

    if _ch1_in is None:
        raise RuntimeError("ADC is not initialized")

    # SIGALRM で読み取りハングを防止
    signal.setitimer(signal.ITIMER_REAL, ADC_READ_TIMEOUT_SEC)
    try:
        v1 = _ch1_in.voltage
        v2 = _ch2_in.voltage
        v3 = _ch3_in.voltage
        return v1, v2, v3
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


# ==========================================
# 計算ヘルパー
# ==========================================
def calc_vref(v: float) -> float:
    """分圧された電圧から Vref (基準電圧) を逆算します。"""
    return v * (R1 + R2) / R2


def calc_temperature(vout: float, vref: float, r_fixed: float) -> tuple[float | None, float | None]:
    """分圧回路の出力電圧からサーミスタ温度 (℃) と抵抗値 (Ω) を算出します。"""
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
    filepath: Path = CSV_PATH,
) -> None:
    """測定結果を temp.csv にアトミックに上書き保存します。"""
    lines = [
        "timestamp,hostname,sensor_name,voltage,HS_data,RS_data,dT_data,dTmax,K_data,samples\n",
        f"{timestamp_str},{hostname},{sensor_name},{round(voltage, 3)},{round(hs_data, 3)},"
        f"{round(rs_data, 3)},{round(dt_data, 3)},{round(dtmax, 3)},{round(k_data, 4)},{samples}\n"
    ]
    try:
        atomic_write_text(filepath, "".join(lines))
    except Exception as e:
        logger.error(f"Failed to write temp.csv atomically: {e}")


# ==========================================
# メインサンプリング・集計ループ
# ==========================================
def main_loop():
    logger.info(f"Starting ADS1115 Temperature Monitor ({SENSOR_NAME})...")
    logger.info(f"  ALLOW_MOCK: {ALLOW_MOCK}")

    # 初回ハードウェア初期化（成功するまで定期リトライ）
    init_retries = 0
    while not init_adc():
        init_retries += 1
        logger.warning(f"ADC init failed. Retrying in 5s (attempt {init_retries})...")
        time.sleep(5)

    sd_notify("READY=1")

    # 前日 dTmax をロード
    prev_dTmax: float | None = get_previous_day_dTmax()
    prev_label = f"{prev_dTmax:.6f}" if prev_dTmax is not None else "N/A (fallback: today's dTmax)"
    logger.info(f"  Previous day dTmax : {prev_label}")

    current_minute = datetime.now().minute
    current_date   = datetime.now().date()

    vref_list: list[float] = []
    t2_list: list[float]   = []
    t3_list: list[float]   = []
    dt_list: list[float]   = []

    # 状態追跡用
    consecutive_adc_errors = 0
    reinit_count = 0
    clock_jump_count = 0
    last_sample_ok_time: float | None = None
    last_summary_time: str | None = None
    last_mono_time = time.monotonic()
    last_wall_time = time.time()

    status_data = {
        "sensor_name": SENSOR_NAME,
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mock_active": _is_mock_active,
        "adc_consecutive_errors": 0,
        "reinit_count": 0,
        "clock_jump_count": 0,
        "last_sample_ok_at": None,
        "last_summary_at": None,
        "status": "running",
    }
    update_status("status_measure.json", status_data)

    while True:
        try:
            # 5秒境界に合わせてスロット待機
            wait_sec = seconds_until_next_slot(offset_sec=0.0, period_sec=5.0, min_gap=0.5)
            time.sleep(wait_sec)

            # 時計ジャンプ検知
            current_mono = time.monotonic()
            current_wall = time.time()
            mono_elapsed = current_mono - last_mono_time
            wall_elapsed = current_wall - last_wall_time
            last_mono_time = current_mono
            last_wall_time = current_wall

            if abs(wall_elapsed - mono_elapsed) > 2.0:
                clock_jump_count += 1
                logger.warning(
                    f"[Clock Jump] Detected clock jump! wall_delta={wall_elapsed:.2f}s, "
                    f"mono_delta={mono_elapsed:.2f}s. Resetting 1-min buffer to prevent invalid summary."
                )
                vref_list.clear()
                t2_list.clear()
                t3_list.clear()
                dt_list.clear()

            # 電圧取得と温度計算
            sample_ok = False
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

                    sample_ok = True
                    consecutive_adc_errors = 0
                    last_sample_ok_time = time.time()
                    time_str = datetime.now().strftime("%H:%M:%S")
                    logger.debug(
                        f"[{time_str}] Vref={vref:.3f}V | T2(Hs)={t2:.3f}C | T3(Rs)={t3:.3f}C | dT={dt:.3f}C"
                    )
                else:
                    logger.warning(f"[Sensor] Out of range reading: v1={v1:.3f}V, v2={v2:.3f}V, v3={v3:.3f}V, vref={vref:.3f}V")
                    consecutive_adc_errors += 1

            except (ADCReadTimeout, Exception) as e:
                consecutive_adc_errors += 1
                if consecutive_adc_errors == 1 or consecutive_adc_errors % 12 == 0:
                    logger.warning(f"[ADC Error] Read sample error ({e}) [consecutive: {consecutive_adc_errors}]")

            # 連続エラー時の自己再初期化（cron @reboot で常駐し続けるための自己回復設計）
            if consecutive_adc_errors >= ADC_REINIT_AFTER and consecutive_adc_errors % ADC_REINIT_AFTER == 0:
                reinit_count += 1
                logger.warning(
                    f"[Recovery] ADC failed {consecutive_adc_errors} times continuously. "
                    f"Attempting to reinitialize ADC hardware (reinit count: {reinit_count})..."
                )
                success = init_adc()
                if success:
                    logger.info("[Recovery] ADC hardware reinitialized successfully.")
                else:
                    logger.error("[Recovery] ADC hardware reinitialization failed. Will retry next cycle.")

            # 分切り替え判定 (毎分 00 秒)
            now = datetime.now()
            if now.minute != current_minute:
                # 日付変更検知 → prev_dTmax を前日値に更新
                if now.date() != current_date:
                    prev_dTmax = get_previous_day_dTmax()
                    current_date = now.date()
                    prev_label = f"{prev_dTmax:.6f}" if prev_dTmax is not None else "N/A (fallback: today's dTmax)"
                    logger.info(f"[New Day] prev_dTmax updated: {prev_label}")

                if len(dt_list) > 0:
                    vref_avg = sum(vref_list) / len(vref_list)
                    t2_avg   = sum(t2_list) / len(t2_list)
                    t3_avg   = sum(t3_list) / len(t3_list)
                    dt_avg   = trim_average(dt_list)
                    samples  = len(dt_list)

                    # dTmax の更新
                    dtmax_val = update_dTmax(dt_avg, now)

                    # K_data: 前日 dTmax を基準（なければ当日 dTmax で代替）
                    ref_dtmax = prev_dTmax if prev_dTmax is not None else dtmax_val
                    k_data = round((ref_dtmax - dt_avg) / dt_avg, 4) if dt_avg != 0.0 else 0.0

                    # タイムスタンプ (00秒固定)
                    timestamp_00 = now.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:00")

                    # temp.csv アトミック上書き保存
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
                    last_summary_time = timestamp_00

                    # 要約ログ
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
                    logger.info(f"[Minute Summary] {json.dumps(summary, ensure_ascii=False)}")
                else:
                    logger.warning("[Minute Summary] No valid ADC samples collected in the last minute. Skipping temp.csv update.")

                # バッファクリア
                vref_list.clear()
                t2_list.clear()
                t3_list.clear()
                dt_list.clear()
                current_minute = now.minute

            # 状態ファイル更新 & systemd ウォッチドッグ通知
            status_data.update({
                "adc_consecutive_errors": consecutive_adc_errors,
                "reinit_count": reinit_count,
                "clock_jump_count": clock_jump_count,
                "last_sample_ok_at": datetime.fromtimestamp(last_sample_ok_time).strftime("%Y-%m-%d %H:%M:%S") if last_sample_ok_time else None,
                "last_summary_at": last_summary_time,
                "mock_active": _is_mock_active,
                "status": "warning" if consecutive_adc_errors > 0 else "running",
            })
            update_status("status_measure.json", status_data)

            # systemd 管理下であればループ生存を通知
            sd_notify("WATCHDOG=1")

        except KeyboardInterrupt:
            logger.info("Measurement stopped by user.")
            break
        except Exception as loop_err:
            # メインループ内の予期せぬ例外でプロセスをクラッシュさせない
            logger.error(f"[Main Loop Error] Unexpected exception: {loop_err}", exc_info=True)
            time.sleep(2)


if __name__ == "__main__":
    main_loop()
