#!/usr/bin/env python3
"""
entropy-rblighter Web 控制台后端
-------------------------------
FastAPI 服务，封装 entropy-arb 的启停、配置、日志、成交数据。
不持有私钥；只读取本地日志/配置/进程状态。

启动：
    cd ~/entropy-rblighter
    source .venv/bin/activate
    bash web.sh            # 或 python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080

安全（重要）：
    默认绑定 0.0.0.0:8080，没有任何鉴权。若服务器有公网 IP，请二选一：
      A) 只在 127.0.0.1 起，然后用 SSH 隧道访问（推荐，见 README）；
      B) 设置环境变量 WEB_TOKEN=一段随机字符串，前端会要求输入令牌，
         之后所有 /api 请求都需带 Authorization: Bearer <WEB_TOKEN>。
"""
import os
import csv
import time
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.routing import APIRouter

APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent
LOG_DIR = REPO_DIR / "logs"

# 时区：界面用北京时间
BJ = timezone(timedelta(hours=8))

# 可选令牌：设置后所有 /api 请求必须带 Bearer
WEB_TOKEN = os.environ.get("WEB_TOKEN", "").strip()

# 允许的日志类型 -> 文件路径
LOG_FILES = {
    "live": LOG_DIR / "live.log",
    "record": LOG_DIR / "record.log",
    "run": LOG_DIR / "run.log",
    "engine": LOG_DIR / "engine.log",
    "trade": LOG_DIR / "trade.log",
    "tune": LOG_DIR / "tune.log",
}

app = FastAPI(title="entropy-rblighter 控制台")


# =============================================================================
# 鉴权（可选）
# =============================================================================
def require_token(authorization: Optional[str] = Header(None)) -> None:
    if not WEB_TOKEN:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "需要令牌：Authorization: Bearer <WEB_TOKEN>")
    if authorization.split(" ", 1)[1].strip() != WEB_TOKEN:
        raise HTTPException(403, "令牌错误")


api = APIRouter(dependencies=[Depends(require_token)])


# =============================================================================
# 工具函数
# =============================================================================
def _run(cmd: str) -> subprocess.CompletedProcess:
    """在仓库目录执行 shell 命令（采集输出，不阻塞）。"""
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=str(REPO_DIR))


def _resolve_cfg() -> Path:
    """配置路径：优先 config.yaml（服务器实际文件名），回退到部署包模板名。"""
    for name in ("config.yaml", "config.entropy-rblighter.yaml"):
        p = REPO_DIR / name
        if p.exists():
            return p
    return REPO_DIR / "config.yaml"


def _read_pid(name: str) -> Optional[int]:
    p = LOG_DIR / f"{name}.pid"
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except Exception:
        return None


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _find_pids(pattern: str) -> List[int]:
    """按命令行模式找 PID（pgrep -f）。"""
    proc = _run(f"pgrep -f {pattern!s}")
    if proc.returncode != 0:
        return []
    return [int(x) for x in proc.stdout.strip().split() if x]


def _proc_uptime(pid: int) -> str:
    proc = _run(f"ps -p {pid} -o etime= 2>/dev/null")
    return proc.stdout.strip() or "?"


def _now_bj() -> str:
    return datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")


def _count_csv(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            return sum(1 for _ in f) - 1  # 减去表头
    except Exception:
        return 0


def _load_config() -> Dict[str, Any]:
    cfg = _resolve_cfg()
    if not cfg.exists():
        raise HTTPException(500, f"config 不存在：{cfg}（请先运行 setup.sh 或手动放置 config.yaml）")
    with cfg.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _thresholds_calibrated(cfg: Dict[str, Any]) -> bool:
    """判断阈值是否还是占位默认值（midline=0, upper=4, lower=4）。"""
    try:
        t = cfg.get("thresholds", {})
        return not (t.get("midline_bps") == 0.0 and t.get("upper_bps") == 4.0 and t.get("lower_bps") == 4.0)
    except Exception:
        return False


# =============================================================================
# 状态
# =============================================================================
@api.get("/status")
def api_status(symbol: str = Query(default="SNDK")) -> Dict[str, Any]:
    """返回智能控制器、实盘、采集三种进程的状态 + 阈值/样本概览。"""

    def _mode_for_run() -> str:
        if _find_pids("main.py --record-only"):
            return "record"
        if _find_pids(f"main.py --symbol {symbol} --hedge"):
            return "trade"
        return "idle"

    run_pid = _read_pid("run")
    live_pid = _read_pid("live")
    record_pid = _read_pid("record")

    run_alive = _pid_alive(run_pid)
    live_alive = _pid_alive(live_pid)
    record_alive = _pid_alive(record_pid)

    # run.sh 守护模式：pid 文件丢了但 pgrep 能找到，也视为运行
    if not run_alive:
        found = _find_pids(f"run.sh.*{symbol}")
        if found:
            run_alive = True
            run_pid = found[0]

    mode = _mode_for_run() if (run_alive or live_alive or record_alive) else "idle"

    # 配置概览（用于前端展示与风险判断）
    try:
        cfg = _load_config()
        thr = cfg.get("thresholds", {})
        thresholds = {
            "midline_bps": thr.get("midline_bps"),
            "upper_bps": thr.get("upper_bps"),
            "lower_bps": thr.get("lower_bps"),
            "calibrated": _thresholds_calibrated(cfg),
        }
        entropy_fee = cfg.get("entropy", {}).get("taker_fee_bps")
        hedge_fee = cfg.get("hedge", {}).get("taker_fee_bps")
    except Exception:
        thresholds = {"calibrated": False}
        entropy_fee = hedge_fee = None

    samples = _count_csv(LOG_DIR / "minutes.csv")
    trades_total = _count_csv(LOG_DIR / "trades.csv")

    return {
        "run": {
            "running": run_alive,
            "pid": run_pid,
            "uptime": _proc_uptime(run_pid) if run_alive and run_pid else None,
            "mode": mode,
        },
        "live": {"running": live_alive, "pid": live_pid,
                 "uptime": _proc_uptime(live_pid) if live_alive and live_pid else None},
        "record": {"running": record_alive, "pid": record_pid,
                   "uptime": _proc_uptime(record_pid) if record_alive and record_pid else None},
        "symbol": symbol,
        "repo": str(REPO_DIR),
        "server_time": _now_bj(),
        "thresholds": thresholds,
        "entropy_fee_bps": entropy_fee,
        "hedge_fee_bps": hedge_fee,
        "samples": samples,
        "trades_total": trades_total,
    }


# =============================================================================
# 配置读写
# =============================================================================
class ConfigPatch(BaseModel):
    thresholds: Optional[Dict[str, float]] = None
    entropy: Optional[Dict[str, Any]] = None
    hedge: Optional[Dict[str, Any]] = None
    sizing: Optional[Dict[str, Any]] = None
    inventory: Optional[Dict[str, Any]] = None
    execution: Optional[Dict[str, Any]] = None
    recorder: Optional[Dict[str, Any]] = None
    logging: Optional[Dict[str, Any]] = None


@api.get("/config")
def api_config() -> Dict[str, Any]:
    """读取当前 config.yaml"""
    return _load_config()


@api.post("/config")
def api_config_patch(patch: ConfigPatch) -> Dict[str, Any]:
    """
    更新 config.yaml（先备份为 config.yaml.bak）。
    只覆盖请求中给出的顶层键；原样覆盖对应键（嵌套字典整体替换）。
    """
    cfg = _load_config()
    changed = []
    for key, value in patch.dict(exclude_unset=True).items():
        if value is None:
            continue
        cfg[key] = value
        changed.append(key)

    cfg_path = _resolve_cfg()
    backup = cfg_path.with_suffix(".yaml.bak")
    try:
        cfg_path.rename(backup)
    except Exception as e:
        raise HTTPException(500, f"备份失败：{e}")

    try:
        with cfg_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    except Exception as e:
        backup.rename(cfg_path)
        raise HTTPException(500, f"写入失败并已回滚：{e}")

    return {"ok": True, "changed": changed, "backup": str(backup), "time": _now_bj()}


# =============================================================================
# 日志
# =============================================================================
@api.get("/logs")
def api_logs(
    type: str = Query(default="live"),
    tail: int = Query(default=100, ge=1, le=1000),
) -> Dict[str, Any]:
    """读取指定日志文件尾部 N 行。"""
    if type not in LOG_FILES:
        raise HTTPException(400, f"未知日志类型：{type}（可选 {list(LOG_FILES)}）")
    path = LOG_FILES[type]
    if not path.exists():
        return {"type": type, "lines": [], "path": str(path), "time": _now_bj()}
    proc = _run(f"tail -n {tail} {path}")
    return {"type": type, "lines": proc.stdout.splitlines(), "path": str(path), "time": _now_bj()}


# =============================================================================
# 成交数据
# =============================================================================
@api.get("/trades")
def api_trades(limit: int = Query(default=20, ge=1, le=200)) -> Dict[str, Any]:
    """读取 logs/trades.csv 最新 N 条成交。"""
    path = LOG_DIR / "trades.csv"
    if not path.exists():
        return {"rows": [], "columns": [], "path": str(path), "time": _now_bj()}
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    rows = rows[-limit:]
    return {"rows": rows, "columns": list(rows[0].keys()) if rows else [],
            "path": str(path), "time": _now_bj()}


# =============================================================================
# 溢价 / 价差分钟线
# =============================================================================
@api.get("/premium")
def api_premium(limit: int = Query(default=120, ge=1, le=1000)) -> Dict[str, Any]:
    """读取 logs/minutes.csv 最新 N 分钟数据。"""
    path = LOG_DIR / "minutes.csv"
    if not path.exists():
        return {"rows": [], "columns": [], "path": str(path), "time": _now_bj()}
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    rows = rows[-limit:]
    return {"rows": rows, "columns": list(rows[0].keys()) if rows else [],
            "path": str(path), "time": _now_bj()}


# =============================================================================
# 控制
# =============================================================================
CONTROL_ACTIONS = {
    "start_smart", "stop_smart",
    "start_trade", "stop_trade",
    "start_record", "stop_record",
    "kill_all",
}


@api.post("/control/{action}")
def api_control(action: str, symbol: str = Query(default="SNDK")) -> Dict[str, Any]:
    """启停控制。start_* 类操作在后台运行，不会阻塞 HTTP。"""
    if action not in CONTROL_ACTIONS:
        raise HTTPException(400, f"未知 action：{action}")

    run_sh = REPO_DIR / "run.sh"
    trade_sh = REPO_DIR / "trade.sh"
    collect_sh = REPO_DIR / "collect.sh"

    if action == "start_smart":
        if not run_sh.exists():
            raise HTTPException(500, f"run.sh 不存在：{run_sh}")
        proc = _run(f"bash {run_sh} --daemon {symbol}")
        return {"ok": True, "action": action, "stdout": proc.stdout, "stderr": proc.stderr, "time": _now_bj()}

    if action == "stop_smart":
        _run("pkill -f 'run.sh' 2>/dev/null || true")
        _run(f"pkill -f 'main.py --symbol {symbol}' 2>/dev/null || true")
        return {"ok": True, "action": action, "time": _now_bj()}

    if action == "start_trade":
        if not trade_sh.exists():
            raise HTTPException(500, f"trade.sh 不存在：{trade_sh}")
        proc = _run(f"bash {trade_sh} {symbol}")
        return {"ok": True, "action": action, "stdout": proc.stdout, "stderr": proc.stderr, "time": _now_bj()}

    if action == "stop_trade":
        _run(f"pkill -f 'main.py --symbol {symbol} --hedge' 2>/dev/null || true")
        return {"ok": True, "action": action, "time": _now_bj()}

    if action == "start_record":
        if not collect_sh.exists():
            raise HTTPException(500, f"collect.sh 不存在：{collect_sh}")
        proc = _run(f"bash {collect_sh} {symbol}")
        return {"ok": True, "action": action, "stdout": proc.stdout, "stderr": proc.stderr, "time": _now_bj()}

    if action == "stop_record":
        _run("pkill -f 'main.py --record-only' 2>/dev/null || true")
        return {"ok": True, "action": action, "time": _now_bj()}

    if action == "kill_all":
        _run("pkill -f 'run.sh' 2>/dev/null || true")
        _run("pkill -f 'main.py --symbol' 2>/dev/null || true")
        _run("pkill -f 'main.py --record-only' 2>/dev/null || true")
        return {"ok": True, "action": action, "time": _now_bj()}

    raise HTTPException(500, "控制分支未命中")


# =============================================================================
# 自动校准阈值（tune.sh 后台运行）
# =============================================================================
@api.post("/tune")
def api_tune(symbol: str = Query(default="SNDK")) -> Dict[str, Any]:
    """
    后台运行 tune.sh：分析已采集样本并自动写回 thresholds 到 config.yaml。
    需要先用 collect.sh / 智能模式采集若干小时样本（logs/minutes.csv）。
    """
    tune_sh = REPO_DIR / "tune.sh"
    if not tune_sh.exists():
        raise HTTPException(500, f"tune.sh 不存在：{tune_sh}")
    _run(f"nohup bash {tune_sh} {symbol} >> {LOG_DIR / 'tune.log'} 2>&1 &")
    return {"ok": True, "action": "tune",
            "note": "已在后台运行 tune.sh（分析样本并写回阈值），完成后查看 /api/config 与 logs/tune.log",
            "time": _now_bj()}


# 把 /api 路由挂到应用
app.include_router(api, prefix="/api")

# 静态文件
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


# =============================================================================
# 入口页面 / 健康检查
# =============================================================================
@app.get("/")
def index() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "index.html")


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"status": "ok", "auth_required": bool(WEB_TOKEN), "time": _now_bj()}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("WEB_PORT", "8080"))
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    uvicorn.run("app:app", host=host, port=port, reload=False)
