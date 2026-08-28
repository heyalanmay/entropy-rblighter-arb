#!/usr/bin/env bash
# Web 控制台启动脚本
#
# 用法：
#   bash web.sh            # 前台启动，端口 8080，绑定 0.0.0.0
#   bash web.sh 8080       # 指定端口
#   WEB_PORT=9000 bash web.sh
#
# 安全（重要）：
#   1) 默认绑定 0.0.0.0，任何人能访问到就能启停交易、花你的钱。
#      强烈建议只绑本地再用 SSH 隧道访问：
#        WEB_HOST=127.0.0.1 bash web.sh
#        # 然后在本机执行：ssh -N -L 8080:127.0.0.1:8080 用户@服务器
#        # 浏览器开 http://localhost:8080
#   2) 或设置令牌（推荐有公网 IP 时）：
#        WEB_TOKEN=$(openssl rand -hex 16) bash web.sh
#      前端首次访问会要求输入该令牌，之后带 Authorization: Bearer <WEB_TOKEN>。
#      （注意：WEB_TOKEN 是进程级、内存中的简单防护，不是完整账号系统；
#        真正的生产级请再加反向代理 + HTTPS + 防火墙白名单。）
set -e
cd "$HOME/entropy-rblighter"
source .venv/bin/activate
PORT="${1:-${WEB_PORT:-8080}}"
HOST="${WEB_HOST:-0.0.0.0}"
mkdir -p logs
pkill -f "uvicorn.*web.app:app" 2>/dev/null || true
sleep 1
nohup uvicorn web.app:app --host "$HOST" --port "$PORT" --app-dir . > logs/web.log 2>&1 &
echo $! > logs/web.pid
echo "[web] 控制台已启动 PID=$!  http://$HOST:$PORT"
echo "[web] 日志：tail -f logs/web.log"
if [ -n "${WEB_TOKEN:-}" ]; then
  echo "[web] 已启用 WEB_TOKEN 鉴权，前端首次访问需输入令牌"
fi
