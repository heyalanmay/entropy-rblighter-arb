#!/usr/bin/env bash
# 实盘启动（真下单、真亏钱！实盘前必须已完成采集+analyze+填阈值+填.env）
# 用法： bash trade.sh SNDK
cd "$HOME/entropy-rblitter"
# shellcheck disable=SC1091
source .venv/bin/activate
SYM="${1:-SNDK}"
python3 main.py --symbol "$SYM" --hedge lighter-rh --cn
