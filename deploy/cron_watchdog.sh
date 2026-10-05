#!/bin/bash
# ==============================================================================
# new_sensor cron 死活監視スクリプト (cron_watchdog.sh)
#
# 【概要】
# - measure_dT.py および post_data.py のプロセス生存を確認します。
# - プロセスが停止している場合、自動的にバックグラウンドで再起動します。
# - crontab に 5分周期などで登録して使用します。
#
# 【crontab 登録例】
#   */5 * * * * /home/plant01/new_sensor/deploy/cron_watchdog.sh >> /home/plant01/new_sensor/logs/watchdog.log 2>&1
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR" || exit 1

PYTHON_CMD="python3"

# 仮想環境 (.venv) がある場合はそちらを優先
if [ -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON_CMD="$SCRIPT_DIR/.venv/bin/python3"
fi

mkdir -p "$SCRIPT_DIR/logs"

# 1. measure_dT.py の死活確認
if ! pgrep -f "python.*measure_dT.py" > /dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WATCHDOG] measure_dT.py is NOT running. Restarting..."
    nohup $PYTHON_CMD -u measure_dT.py >> logs/measure_stdout.log 2>&1 &
fi

# 2. post_data.py の死活確認
if ! pgrep -f "python.*post_data.py" > /dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WATCHDOG] post_data.py is NOT running. Restarting..."
    nohup $PYTHON_CMD -u post_data.py >> logs/post_stdout.log 2>&1 &
fi
