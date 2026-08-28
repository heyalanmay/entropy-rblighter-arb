#!/usr/bin/env bash
# ============================================================================
#  Entropy ↔ rblighter 一键部署脚本（在新服务器上运行）
#  用法：把整个 entropy-rblighter-deploy 文件夹上传到新服务器，
#        进入该文件夹后执行：  bash setup.sh
#  脚本会：装依赖 → 克隆引擎到 ~/entropy-rblighter → 装 Python 环境
#          → 写入专属 config 与 .env 模板 → 生成启动脚本
# ============================================================================
set -e

PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$HOME/entropy-rblighter"

echo "==> [1/5] 安装系统依赖（python3 / venv / git）..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

echo "==> [2/5] 克隆开源引擎到 $REPO_DIR ..."
if [ -d "$REPO_DIR" ]; then
  echo "    目录已存在，跳过克隆（如需更新请手动 git pull）"
else
  git clone https://github.com/your-quantguy/entropy-arb.git "$REPO_DIR"
fi

echo "==> [3/5] 创建 Python 虚拟环境并安装依赖..."
cd "$REPO_DIR"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-live.txt

echo "==> [4/5] 写入专属配置与密钥模板..."
cp "$PKG_DIR/config.entropy-rblighter.yaml" "$REPO_DIR/config.yaml"
cp "$PKG_DIR/.env.entropy-rblighter"        "$REPO_DIR/.env"
cp "$PKG_DIR/collect.sh"                    "$REPO_DIR/collect.sh"
cp "$PKG_DIR/trade.sh"                      "$REPO_DIR/trade.sh"
chmod +x "$REPO_DIR/collect.sh" "$REPO_DIR/trade.sh"

echo "==> [5/5] 完成。"
echo ""
echo "下一步（务必按顺序）："
echo "  1) 在 Hyperliquid 新建 agent 钱包 + 在 Robinhood 链 Lighter 新建账户并生成 API key"
echo "  2) 编辑 $REPO_DIR/.env ，填入 5 个密钥，并给两个账户分别充 USDC / USDG"
echo "  3) 先采集行情（不花钱）：  bash $REPO_DIR/collect.sh SNDK"
echo "  4) 采集几小时后分析阈值：  cd $REPO_DIR && source .venv/bin/activate && python3 tools/analyze.py --fees-bps 2.5"
echo "  5) 把 analyze 输出的 midline/upper/lower 填进 $REPO_DIR/config.yaml 顶部 thresholds"
echo "  6) 实盘启动：  bash $REPO_DIR/trade.sh SNDK"
echo ""
echo "（建议用 tmux 运行，断线不死：tmux new -s arb → 跑 trade.sh → Ctrl+B D 脱离）"
