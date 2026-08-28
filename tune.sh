#!/usr/bin/env bash
# ============================================================================
#  阈值自动测算并写入配置（在 ~/entropy-rblighter 目录内运行）
#  用法：
#    bash tune.sh            # 用默认 --fees-bps 2.5 测算并写入
#    bash tune.sh 2.5        # 显式指定往返手续费假设（bps）
#
#  做什么：运行 tools/analyze.py，提取"建议起点"的 midline/upper/lower，
#          自动写回 config.yaml 的 thresholds 三行。
#  不做：不会动 sizing / inventory / 仓位上限，那些是低磨损调优项，已在 config 里设好。
# ============================================================================
set -e

REPO_DIR="$HOME/entropy-rblighter"
cd "$REPO_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

FEES="${1:-2.5}"
echo "==> 运行 analyze.py --fees-bps $FEES ..."
python3 tools/analyze.py --fees-bps "$FEES" | tee /tmp/entropy_rblighter_analyze.txt

echo "==> 提取 thresholds 建议值 ..."
MID=$(grep -oE 'midline_bps: [-0-9.]+' /tmp/entropy_rblighter_analyze.txt | head -1 | grep -oE '[-0-9.]+$')
UP=$(grep -oE 'upper_bps: [-0-9.]+'   /tmp/entropy_rblighter_analyze.txt | head -1 | grep -oE '[-0-9.]+$')
LOW=$(grep -oE 'lower_bps: [-0-9.]+'  /tmp/entropy_rblighter_analyze.txt | head -1 | grep -oE '[-0-9.]+$')

if [ -z "$MID" ] || [ -z "$UP" ] || [ -z "$LOW" ]; then
  echo "❌ 没能从 analyze 输出里解析出 midline/upper/lower，请检查上面的输出是否完整。"
  exit 1
fi

echo "     midline=$MID  upper=$UP  lower=$LOW"
echo "==> 写回 config.yaml ..."
python3 - "$MID" "$UP" "$LOW" <<'PY'
import sys, re
mid, up, low = sys.argv[1], sys.argv[2], sys.argv[3]
p = "config.yaml"
s = open(p).read()
# 只替换 thresholds 下的三个值（保证只命中一次、不碰注释里的同名词）
s = re.sub(r'(\n\s*midline_bps:\s*)[-0-9.]+', r'\1'+mid, s, count=1)
s = re.sub(r'(\n\s*upper_bps:\s*)[-0-9.]+',  r'\1'+up,  s, count=1)
s = re.sub(r'(\n\s*lower_bps:\s*)[-0-9.]+',  r'\1'+low, s, count=1)
open(p, 'w').write(s)
print("✅ 已写入 thresholds: midline=%s  upper=%s  lower=%s" % (mid, up, low))
PY

echo ""
echo "下一步：确认仓位上限后实盘 →  bash trade.sh SNDK"
echo "（仍建议先小仓位验证：把 config 里 entropy/hedge 的 max_position_usd 调小）"
