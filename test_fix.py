"""new_sensor 改修機能の総合ユニット・統合テストスクリプト"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

# カレントディレクトリ設定
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import common
import send_queue
import save_data
import post_data


class TestCommon(unittest.TestCase):
    def test_atomic_write_and_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            common.atomic_write_text(test_file, "hello atomic")
            self.assertTrue(test_file.exists())
            self.assertEqual(test_file.read_text(), "hello atomic")

            json_file = Path(tmpdir) / "test.json"
            common.atomic_write_json(json_file, {"key": "value"})
            data = json.loads(json_file.read_text())
            self.assertEqual(data["key"], "value")

    def test_seconds_until_next_slot(self):
        wait = common.seconds_until_next_slot(offset_sec=0.0, period_sec=5.0, min_gap=0.5)
        self.assertGreaterEqual(wait, 0.5)
        self.assertLessEqual(wait, 5.5)

        wait_min = common.seconds_until_next_slot(offset_sec=20.0, period_sec=60.0, min_gap=0.5)
        self.assertGreaterEqual(wait_min, 0.5)
        self.assertLessEqual(wait_min, 60.5)


class TestSendQueueAndDeduplication(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "pending.db"
        self.state_file = Path(self.test_dir) / "post_state.json"

        # send_queue のパスをモック
        self.patch_db = patch.object(send_queue, "DB_PATH", self.db_path)
        self.patch_data_dir = patch.object(send_queue, "DATA_DIR", Path(self.test_dir))
        self.patch_state = patch.object(send_queue, "STATE_FILE", self.state_file)
        self.patch_db.start()
        self.patch_data_dir.start()
        self.patch_state.start()

    def tearDown(self):
        self.patch_db.stop()
        self.patch_data_dir.stop()
        self.patch_state.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_queue_and_deduplication(self):
        queue = send_queue.SendQueue(max_rows=10)
        self.assertIsNone(queue.last_posted_timestamp)

        # 1. 未送信ペイロードを enqueue
        payload1 = {"timestamp": "2026-10-05 10:00:00", "val": 1}
        self.assertTrue(queue.enqueue("2026-10-05 10:00:00", payload1))
        self.assertEqual(queue.get_pending_count(), 1)

        # 2. 同一タイムスタンプの重複 enqueue は無視される
        self.assertFalse(queue.enqueue("2026-10-05 10:00:00", payload1))
        self.assertEqual(queue.get_pending_count(), 1)

        # 3. 送信成功マーク
        queue.mark_as_posted("2026-10-05 10:00:00")
        self.assertEqual(queue.last_posted_timestamp, "2026-10-05 10:00:00")
        self.assertEqual(queue.get_pending_count(), 0)  # キューから削除されている

        # 4. 送信済みタイムスタンプの重複チェック
        self.assertTrue(queue.is_already_posted("2026-10-05 10:00:00"))
        self.assertTrue(queue.is_already_posted("2026-10-05 09:59:00"))  # 過去もブロック
        self.assertFalse(queue.is_already_posted("2026-10-05 10:01:00")) # 未来はOK

        # 5. 送信済みタイムスタンプの enqueue 抑止
        self.assertFalse(queue.enqueue("2026-10-05 10:00:00", payload1))

        # 6. 新しいインスタンス（再起動シミュレーション）で永続化確認
        queue2 = send_queue.SendQueue(max_rows=10)
        self.assertEqual(queue2.last_posted_timestamp, "2026-10-05 10:00:00")
        self.assertTrue(queue2.is_already_posted("2026-10-05 10:00:00"))


class TestSaveData(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_and_prevent_duplicate_row(self):
        row = {
            "timestamp": "2026-10-05 12:00:00",
            "hostname": "pi-test",
            "sensor_name": "new01",
            "voltage": "3.3",
            "HS_data": "25.0",
            "RS_data": "24.0",
            "dT_data": "1.0",
            "dTmax": "1.5",
            "K_data": "0.123",
            "samples": "12",
        }
        # 1回目の書き込み
        res1 = save_data.save_data_csv(row, data_dir=self.test_dir)
        self.assertTrue(res1)

        csv_file = Path(self.test_dir) / "new01_2026-10-05.csv"
        self.assertTrue(csv_file.exists())
        lines = csv_file.read_text().strip().split("\n")
        self.assertEqual(len(lines), 2)  # ヘッダ + 1行

        # 2回目（同一タイムスタンプ）の書き込み -> スキップされて行数は増えない
        res2 = save_data.save_data_csv(row, data_dir=self.test_dir)
        self.assertTrue(res2)
        lines2 = csv_file.read_text().strip().split("\n")
        self.assertEqual(len(lines2), 2)


class TestPostDataCycle(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.temp_csv = Path(self.test_dir) / "temp.csv"
        self.patch_csv = patch.object(post_data, "CSV_PATH", self.temp_csv)
        self.patch_csv.start()

        # SendQueue のモック準備
        self.db_path = Path(self.test_dir) / "pending.db"
        self.state_file = Path(self.test_dir) / "post_state.json"
        self.patch_db = patch.object(send_queue, "DB_PATH", self.db_path)
        self.patch_data_dir = patch.object(send_queue, "DATA_DIR", Path(self.test_dir))
        self.patch_state = patch.object(send_queue, "STATE_FILE", self.state_file)
        self.patch_save_dir = patch.object(save_data, "DATA_DIR", str(Path(self.test_dir) / "data"))
        self.patch_db.start()
        self.patch_data_dir.start()
        self.patch_state.start()
        self.patch_save_dir.start()

    def tearDown(self):
        self.patch_csv.stop()
        self.patch_db.stop()
        self.patch_data_dir.stop()
        self.patch_state.stop()
        self.patch_save_dir.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def write_temp_csv(self, ts="2026-10-05 13:00:00"):
        content = (
            "timestamp,hostname,sensor_name,voltage,HS_data,RS_data,dT_data,dTmax,K_data,samples\n"
            f"{ts},test-host,new01,3.3,25.0,24.0,1.0,1.5,0.1,12\n"
        )
        self.temp_csv.write_text(content)

    @patch("post_data.execute_post")
    def test_post_cycle_success_and_deduplication(self, mock_execute):
        mock_execute.return_value = (True, 200, None)
        queue = send_queue.SendQueue()

        self.write_temp_csv("2026-10-05 14:00:00")
        res1 = post_data.post_cycle(queue, self.temp_csv)
        self.assertTrue(res1["posted"])
        self.assertEqual(queue.last_posted_timestamp, "2026-10-05 14:00:00")
        self.assertEqual(mock_execute.call_count, 1)

        # 2回目（未更新で同じタイムスタンプ） -> 送信がスキップされる
        res2 = post_data.post_cycle(queue, self.temp_csv)
        self.assertFalse(res2["posted"])
        self.assertEqual(res2["status"], "already_posted")
        self.assertEqual(mock_execute.call_count, 1)  # 呼び出し回数は増えない

    @patch("post_data.execute_post")
    def test_post_cycle_failure_and_resend(self, mock_execute):
        # 1回目は失敗
        mock_execute.return_value = (False, None, "Connection error")
        queue = send_queue.SendQueue()

        self.write_temp_csv("2026-10-05 15:00:00")
        res1 = post_data.post_cycle(queue, self.temp_csv)
        self.assertFalse(res1["posted"])
        self.assertEqual(res1["status"], "failed")
        self.assertEqual(queue.get_pending_count(), 1)  # キューに保存された

        # 2回目は成功に転じる（新しいタイムスタンプ）
        mock_execute.return_value = (True, 200, None)
        self.write_temp_csv("2026-10-05 15:01:00")
        res2 = post_data.post_cycle(queue, self.temp_csv)
        self.assertTrue(res2["posted"])
        self.assertEqual(queue.get_pending_count(), 0)  # 再送されてキューが空になった



if __name__ == "__main__":
    unittest.main()
