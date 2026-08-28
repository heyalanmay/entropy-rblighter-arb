#!/usr/bin/env bash
# 采集行情（不花钱、不需要密钥也能跑，用于后面 analyze 算阈值）
# 用法： bash collect.sh SNDK
set -e
cd "$HOME/entropy-rblighter"
source .venv/bin/activate
SYM="${1:-SNDK}"
mkdir -p logs
pkill -f "main.py --record-only --symbol $SYM" 2>/dev/null || true
sleep 1
nohup python3 main.py --record-only --symbol "$SYM" --hedge lighter-rh --cn \
  > logs/record.log 2>&1 &
echo $! > logs/record.pid
echo "[collect] 采集已后台启动 PID=$!  symbol=$SYM  日志：tail -f logs/record.log"