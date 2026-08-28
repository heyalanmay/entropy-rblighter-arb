#!/usr/bin/env bash
# 实盘启动（真下单、真亏钱！实盘前必须已完成采集+analyze+填阈值+填.env）
# 用法： bash trade.sh SNDK
set -e
cd "$HOME/entropy-rblighter"
source .venv/bin/activate
SYM="${1:-SNDK}"
mkdir -p logs
pkill -f "main.py --symbol $SYM --hedge" 2>/dev/null || true
sleep 1
nohup python3 main.py --symbol "$SYM" --hedge lighter-rh --cn \
  > logs/live.log 2>&1 &
echo $! > logs/live.pid
echo "[trade] 实盘已后台启动 PID=$!  symbol=$SYM  日志：tail -f logs/live.log"