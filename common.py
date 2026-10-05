"""new_sensor 共通ユーティリティモジュール

【機能】
- 回転ログ（RotatingFileHandler + StreamHandler）
- アトミックファイル書き込み（temp.csv, 状態JSON等）
- プロセス状態ファイル更新 (status_*.json)
- 時計ジャンプ耐性付きスロット待機計算
- systemd sd_notify 通知（環境変数 NOTIFY_SOCKET が存在する場合のみ送信）
"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from typing import Any, Dict


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"


def setup_logger(
    name: str,
    log_file: str | None = None,
    level: int = logging.INFO,
    max_bytes: int = 2 * 1024 * 1024,  # 2MB
    backup_count: int = 5,
) -> logging.Logger:
    """指定された名前のロガーを設定して返します。

    :param name: ロガー識別名
    :param log_file: ログファイル名（None の場合はファイル出力なし）
    :param level: ログレベル
    :param max_bytes: ローテーションサイズ（バイト）
    :param backup_count: 世代保持数
    :return: logging.Logger インスタンス
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 既にハンドラが設定されている場合は再設定を防ぐ
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. コンソール出力（標準出力）
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    # 2. ファイル出力（指定時）
    if log_file:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        file_path = LOGS_DIR / log_file
        file_handler = RotatingFileHandler(
            filename=str(file_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def atomic_write_text(filepath: str | Path, content: str, encoding: str = "utf-8") -> None:
    """ファイルをアトミックに書き込みます（一時ファイル作成 -> fsync -> os.replace）。

    :param filepath: 書き込み先パス
    :param content: 書き込む文字列
    :param encoding: 文字コード
    """
    path = Path(filepath).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=str(path.parent),
        encoding=encoding,
        delete=False,
        suffix=".tmp",
    ) as tf:
        tf.write(content)
        tf.flush()
        os.fsync(tf.fileno())
        tmp_name = tf.name

    os.replace(tmp_name, str(path))


def atomic_write_json(filepath: str | Path, data: Any, encoding: str = "utf-8") -> None:
    """JSON データをアトミックに書き込みます。"""
    content = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_text(filepath, content, encoding=encoding)


def update_status(filename: str, status_data: Dict[str, Any]) -> None:
    """プロセス状態 JSON ファイルをアトミックに更新します。

    :param filename: 例: 'status_measure.json', 'status_post.json'
    :param status_data: 状態辞書
    """
    status_data = dict(status_data)
    status_data["last_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = BASE_DIR / filename
    try:
        atomic_write_json(path, status_data)
    except Exception as e:
        # 状態保存エラーで本体プロセスを止めない
        pass


def seconds_until_next_slot(
    offset_sec: float,
    period_sec: float,
    min_gap: float = 0.5,
) -> float:
    """指定周期のスロット時刻までの残り秒数を計算します。

    :param offset_sec: スロットの秒オフセット (例: 毎分20秒なら 20.0, 5秒周期なら 0.0)
    :param period_sec: 周期秒 (例: 1分なら 60.0, 5秒なら 5.0)
    :param min_gap: 最小確保間隔（NTP等の微小な時計揺れで直前の同一枠を重複実行するのを防ぐ）
    :return: 待機秒数 (常に min_gap <= 待機秒数 <= period_sec + min_gap)
    """
    now = time.time()
    wait = (offset_sec - now) % period_sec
    if wait < min_gap:
        wait += period_sec
    return wait


def sd_notify(state_str: str) -> bool:
    """systemd の NOTIFY_SOCKET に対し状態文字列を送信します。
    systemd 管理下でない場合（cron や直接実行）は何もしません。

    :param state_str: 例: 'READY=1', 'WATCHDOG=1'
    :return: 送信成功時 True, 送信不要または失敗時 False
    """
    socket_path = os.getenv("NOTIFY_SOCKET")
    if not socket_path:
        return False

    try:
        if socket_path.startswith("@"):
            socket_path = "\0" + socket_path[1:]

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(state_str.encode("utf-8"), socket_path)
            return True
        finally:
            sock.close()
    except Exception:
        return False
