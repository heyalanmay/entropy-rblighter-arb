#!/usr/bin/env bash
# ============================================================================
#  run.sh — Entropy↔rblighter 常驻智能控制器（推荐用它，而不是裸跑 trade.sh）
#
#  三个核心能力：
#   1) 【美股开盘避让】北京时间 21:00-22:00 高波动时段：自动切到 --record-only
#      （只采集行情、绝不下单），避开价差失真/滑点放大的危险期；其余时段正常实盘。
#      —— 这与 entropy-arb 官方风险说明一致：股权类永续（如 SNDK）在美股交易时段，
#         不同交易所的预言机机制差异大，官方建议“放宽阈值或不交易”。
#   2) 【人类化 / 反女巫 / 下单数量随机】
#      引擎启动时只读一次 config（无热加载），本身没有“每笔随机下单位”的能力。
#      所以本控制器靠【周期性换档重启】注入新的随机下单参数，让单笔名义在区间里浮动：
#        · 每次换档随机化：take_fraction / max_order_notional_usd /
#          min_order_notional_usd / cooldown_sec / premium_persist_sec
#        · 单笔名义 = clamp(take_fraction × 盘口可套利深度, min_order, max_order)
#          → 即便同一档期内，盘口深度每刻不同，单笔大小也自然浮动；
#            跨档期（默认每 15-30 分钟）参数又换一批，浮动区间整体平移。
#      这比“写死固定值”更像真实交易员：某段时间偏小额、某段时间偏大额、
#      下单位从不重复同一个数，规避项目方对“机械刷量 / 女巫模式”的识别。
#   3) 【崩溃自拉起】进程掉了自动重启；时段切换也会自动在两种模式间切。
#
#  重要说明：
#   - 在“只采集”时段，之前已开出的持仓不会被主动管理（不加减仓、不平仓），
#     直到 22:00 回到实盘模式后，程序重新从链上读取持仓并继续管理。这是刻意的：
#     美股开盘波动大，不做交易就是最稳的选择。
#   - 本 bot 做的是真实跨所套利（一边买一边卖），本身不是自成交/假量，
#     不会触发“假量女巫”规则；上面的抖动只是让行为别像个时钟。
#
#  用法（强烈建议放进 tmux，断线不死）：
#      tmux new -s arb
#      bash run.sh SNDK
#      Ctrl+B 然后 D   # 脱离，程序继续跑
#  停止： pkill -f "run.sh" ; pkill -f "main.py --symbol SNDK"
# ============================================================================
set -euo pipefail

SYMBOL="${1:-SNDK}"
HEDGE="lighter-rh"
REPO_DIR="$HOME/entropy-rblighter"
CFG="$REPO_DIR/config.yaml"
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"

# ----------------------------- 可配置项 -------------------------------------
PAUSE_START_HOUR=21   # 北京时间：开始“只采集”的小时（含），即 21:00
PAUSE_END_HOUR=22     # 北京时间：结束“只采集”的小时（不含），即 22:00 起恢复交易
HUMANIZE=1            # 是否启用下单参数随机抖动（1=启用，0=关闭并固定为 config 值）

# 下单参数抖动安全区间（每次换档取值不同，制造“手感”差异；都在低磨损安全区内）
TF_MIN=0.15; TF_MAX=0.28        # sizing.take_fraction（吃盘口顶部深度的比例）
MO_MIN=20;  MO_MAX=32           # sizing.max_order_notional_usd（单笔名义上限 $）
MINO_MIN=5; MINO_MAX=12         # sizing.min_order_notional_usd（单笔名义下限 $，本次也纳入随机）
CD_MIN=0.5;  CD_MAX=2.5         # execution.cooldown_sec（两次下单最小间隔/秒）
PP_MIN=1;    PP_MAX=3           # execution.premium_persist_sec（信号需持续秒数）

# 换档重启间隔（秒）：交易时段内每过一段时间就重启 bot 并注入新随机参数。
# 间隔本身也随机，避免“固定周期”这种机械特征。
# ⚠️ 太短（如 <300）会频繁断 WebSocket、重读持仓；太长则单笔大小长期不变。
#    默认 900~1800（15~30 分钟）。想更“密”可调小，但别低于 600（10 分钟）。
RESHUFFLE_MIN=900
RESHUFFLE_MAX=1800

# ----------------------------- 工具函数 -------------------------------------
in_pause() {
  local h
  h=$(TZ='Asia/Shanghai' date +%H)
  [ "$h" -ge "$PAUSE_START_HOUR" ] && [ "$h" -lt "$PAUSE_END_HOUR" ]
}

randf() { awk -v a="$1" -v b="$2" 'BEGIN{srand(); printf "%.2f", a+rand()*(b-a)}'; }
randi() { awk -v a="$1" -v b="$2" 'BEGIN{srand(); printf "%d", a+int(rand()*(b-a+1))}'; }

apply_humanize() {
  # 仅在“真正（重新）启动实盘”时调用；改写 config.yaml 对应行（保留原缩进）
  local tf mo mino cd pp
  tf=$(randf "$TF_MIN" "$TF_MAX")
  mo=$(randi "$MO_MIN" "$MO_MAX")
  mino=$(randi "$MINO_MIN" "$MINO_MAX")
  # 保证 min <= max（极端情况下随机到的 min>max 会让引擎不下单）
  [ "$mino" -gt "$mo" ] && mino=$mo
  cd=$(randf "$CD_MIN" "$CD_MAX")
  pp=$(randi "$PP_MIN" "$PP_MAX")
  sed -i -E "s/^  take_fraction:.*/  take_fraction: $tf/" "$CFG"
  sed -i -E "s/^  max_order_notional_usd:.*/  max_order_notional_usd: $mo/" "$CFG"
  sed -i -E "s/^  min_order_notional_usd:.*/  min_order_notional_usd: $mino/" "$CFG"
  sed -i -E "s/^  cooldown_sec:.*/  cooldown_sec: $cd/" "$CFG"
  sed -i -E "s/^  premium_persist_sec:.*/  premium_persist_sec: $pp/" "$CFG"
  echo "  [$(date)] 人类化抖动：take_fraction=$tf  max_order=$mo  min_order=$mino  cooldown=$cd  persist=$pp" >> "$LOG_DIR/run.log"
}

is_running() { pgrep -f "$1" >/dev/null 2>&1; }

launch_trade() {
  cd "$REPO_DIR" && source .venv/bin/activate
  nohup python3 main.py --symbol "$SYMBOL" --hedge "$HEDGE" --cn \
    >> "$LOG_DIR/trade.log" 2>&1 &
}

start_record() {
  if is_running "main.py --record-only"; then return 0; fi
  pkill -f "main.py --symbol $SYMBOL --hedge" 2>/dev/null || true   # 停掉实盘，切采集
  sleep 2
  cd "$REPO_DIR" && source .venv/bin/activate
  nohup python3 main.py --record-only --symbol "$SYMBOL" --hedge "$HEDGE" --cn \
    >> "$LOG_DIR/record.log" 2>&1 &
  echo "  [$(date)] >>> 进入采集模式（--record-only，不交易）" >> "$LOG_DIR/run.log"
}

start_trade() {
  if is_running "main.py --symbol $SYMBOL --hedge"; then return 0; fi
  pkill -f "main.py --record-only" 2>/dev/null || true              # 停掉采集，切实盘
  sleep 2
  [ "$HUMANIZE" = "1" ] && apply_humanize
  launch_trade
  echo "  [$(date)] >>> 进入实盘模式" >> "$LOG_DIR/run.log"
}

reshuffle_trade() {
  # 换档重启：停掉当前实盘 → 注入新随机参数 → 重新启动
  pkill -f "main.py --symbol $SYMBOL --hedge" 2>/dev/null || true
  sleep 3
  [ "$HUMANIZE" = "1" ] && apply_humanize
  launch_trade
  echo "  [$(date)] >>> 换档重启（注入新随机下单参数）" >> "$LOG_DIR/run.log"
}

# ----------------------------- 主循环 ---------------------------------------
echo "run.sh 启动：符号=$SYMBOL  对冲=$HEDGE  暂停窗口=${PAUSE_START_HOUR}:00-${PAUSE_END_HOUR}:00(北京时间)  人类化=$HUMANIZE"
echo "           换档间隔=${RESHUFFLE_MIN}~${RESHUFFLE_MAX}秒随机  用法：tmux new -s arb → bash run.sh $SYMBOL → Ctrl+B D 脱离"
next_reshuffle=$(($(date +%s) + $(randi "$RESHUFFLE_MIN" "$RESHUFFLE_MAX")))
while true; do
  now=$(date +%s)
  if in_pause; then
    start_record
    # pause 期间不计时；回到交易时段第一次启动会重置 next_reshuffle
  else
    if is_running "main.py --symbol $SYMBOL --hedge"; then
      # 已在实盘：到达换档点则重启注入新随机参数；否则保持
      if [ "$HUMANIZE" = "1" ] && [ "$now" -ge "$next_reshuffle" ]; then
        reshuffle_trade
        next_reshuffle=$(( now + $(randi "$RESHUFFLE_MIN" "$RESHUFFLE_MAX") ))
      fi
    else
      start_trade
      next_reshuffle=$(( now + $(randi "$RESHUFFLE_MIN" "$RESHUFFLE_MAX") ))
    fi
  fi
  sleep 30
done
