#!/usr/bin/env bash
# 采集行情（不花钱、不需要密钥也能跑，用于后面 analyze 算阈值）
# 用法： bash collect.sh SNDK
cd "$HOME/entropy-rblitter"
# shellcheck disable=SC1091
source .venv/bin/activate
SYM="${1:-SNDK}"
python3 main.py --record-only --symbol "$SYM" --hedge lighter-rh --cn
