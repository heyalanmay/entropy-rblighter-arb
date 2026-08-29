#!/usr/bin/env python3
"""
entropy-rblighter Web 控制台后端
-------------------------------
FastAPI 服务，封装 entropy-arb 的启停、配置、日志、成交数据、任务管理。
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
import json
import uuid
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncio
import yaml
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.routing import APIRouter

APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent
LOG_DIR = REPO_DIR / "logs"
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TASKS_FILE = DATA_DIR / "tasks.json"

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
def _run(cmd: str, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    """在仓库目录执行 shell 命令（采集输出，不阻塞）"""
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=str(REPO_DIR), env=merged)


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


def _save_config(cfg: Dict[str, Any]) -> Path:
    """写 config.yaml，先备份。返回 config 路径。"""
    cfg_path = _resolve_cfg()
    backup = cfg_path.with_suffix(".yaml.bak")
    try:
        if cfg_path.exists():
            cfg_path.rename(backup)
    except Exception as e:
        raise HTTPException(500, f"备份失败：{e}")
    try:
        with cfg_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    except Exception as e:
        if backup.exists():
            backup.rename(cfg_path)
        raise HTTPException(500, f"写入失败并已回滚：{e}")
    return cfg_path


def _thresholds_calibrated(cfg: Dict[str, Any]) -> bool:
    """判断阈值是否还是占位默认值（midline=0, upper=4, lower=4）。"""
    try:
        t = cfg.get("thresholds", {})
        return not (t.get("midline_bps") == 0.0 and t.get("upper_bps") == 4.0 and t.get("lower_bps") == 4.0)
    except Exception:
        return False


# =============================================================================
# 任务持久化
# =============================================================================
def _load_tasks() -> List[Dict[str, Any]]:
    if not TASKS_FILE.exists():
        return []
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save_tasks(tasks: List[Dict[str, Any]]) -> None:
    TASKS_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")


def _gen_task_id(symbol: str) -> str:
    return f"{symbol.lower()}-{datetime.now(BJ).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4]}"


def _find_task(task_id: str) -> Optional[Dict[str, Any]]:
    for t in _load_tasks():
        if t.get("id") == task_id:
            return t
    return None


def _task_status(task: Dict[str, Any]) -> str:
    """根据进程状态推导任务状态。"""
    symbol = task.get("symbol", "SNDK")
    run_alive = bool(_find_pids(f"run.sh.*{symbol}") or _find_pids(f"run.sh {symbol}"))
    live_alive = bool(_find_pids(f"main.py --symbol {symbol} --hedge"))
    record_alive = bool(_find_pids(f"main.py --record-only --symbol {symbol}"))
    if run_alive:
        return "running"
    if live_alive:
        return "live"
    if record_alive:
        return "record"
    return task.get("status", "idle")


def _apply_task_to_config(task: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    """把任务参数写入 config 字典（保留其他键）。"""
    cfg.setdefault("thresholds", {})
    cfg.setdefault("entropy", {})
    cfg.setdefault("hedge", {})
    cfg.setdefault("sizing", {})
    cfg.setdefault("execution", {})

    target = float(task.get("target_profit_bps", 2.0))
    cfg["thresholds"]["midline_bps"] = float(task.get("midline_bps", 0.0))
    cfg["thresholds"]["upper_bps"] = target
    cfg["thresholds"]["lower_bps"] = target

    max_pos = float(task.get("max_position_usd", 100.0))
    cfg["entropy"]["taker_fee_bps"] = float(task.get("entropy_fee_bps", 2.5))
    cfg["entropy"]["max_position_usd"] = max_pos

    cfg["hedge"]["exchange"] = "lighter-rh"
    cfg["hedge"]["taker_fee_bps"] = float(task.get("hedge_fee_bps", 0.0))
    cfg["hedge"]["max_position_usd"] = max_pos
    cfg["hedge"]["max_orders_per_min"] = int(task.get("max_orders_per_min", 35))

    cfg["sizing"]["take_fraction"] = float(task.get("take_fraction", 0.2))
    cfg["sizing"]["max_order_notional_usd"] = float(task.get("order_size_usd", 25.0))
    cfg["sizing"]["min_order_notional_usd"] = float(task.get("min_order_size_usd", 5.0))

    cfg["execution"]["leg_slippage_bps"] = float(task.get("leg_slippage_bps", 30.0))
    cfg["execution"]["hedge_slippage_bps"] = float(task.get("hedge_slippage_bps", 15.0))
    cfg["execution"]["cooldown_sec"] = float(task.get("cooldown_sec", 1.0))
    cfg["execution"]["premium_persist_sec"] = int(task.get("premium_persist_sec", 2))


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
        max_order = cfg.get("sizing", {}).get("max_order_notional_usd")
    except Exception:
        cfg = {}
        thresholds = {"calibrated": False}
        entropy_fee = hedge_fee = max_order = None

    samples = _count_csv(LOG_DIR / "minutes.csv")
    trades_total = _count_csv(LOG_DIR / "trades.csv")

    # 当前 symbol 对应的任务
    active_task = None
    for t in _load_tasks():
        if t.get("symbol", "SNDK") == symbol:
            active_task = {
                "id": t["id"],
                "name": t.get("name"),
                "status": _task_status(t),
                "target_profit_bps": t.get("target_profit_bps"),
                "order_size_usd": t.get("order_size_usd"),
                "live_start_samples": t.get("live_start_samples", 0),
            }
            break

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
        "max_order_usd": max_order,
        "samples": samples,
        "trades_total": trades_total,
        "active_task": active_task,
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

    cfg_path = _save_config(cfg)
    return {"ok": True, "changed": changed, "path": str(cfg_path), "time": _now_bj()}


# =============================================================================
# 任务管理
# =============================================================================
class TaskCreate(BaseModel):
    name: Optional[str] = Field(default=None, description="任务名称，默认 交易对套利")
    symbol: str = Field(default="SNDK", description="交易对，如 SNDK")
    market_index: Optional[str] = Field(default=None, description="Hyperliquid market index，仅作展示")
    target_profit_bps: float = Field(default=2.0, ge=0.1, description="目标盈利 bps（同时写入 upper/lower）")
    midline_bps: float = Field(default=0.0, description="中枢 bps")
    order_size_usd: float = Field(default=25.0, ge=1.0, description="单笔最大下单名义金额 USD")
    min_order_size_usd: float = Field(default=5.0, ge=1.0, description="单笔最小下单名义金额 USD")
    max_position_usd: float = Field(default=100.0, ge=10.0, description="两边最大仓位 USD")
    take_fraction: float = Field(default=0.2, ge=0.01, le=1.0, description="吃盘口深度比例")
    cooldown_sec: float = Field(default=1.0, ge=0.0, description="两次下单最小间隔秒")
    premium_persist_sec: int = Field(default=2, ge=1, description="溢价信号需持续秒数")
    sliding_window_samples: int = Field(default=100000, ge=1000, description="analyze 滑动窗口样本数（ tune 参考）")
    live_start_samples: int = Field(default=10000, ge=0, description="允许实盘的最小分钟样本数")
    max_orders_per_min: int = Field(default=35, ge=1, description="对冲侧每分钟最大订单数")
    entropy_fee_bps: float = Field(default=2.5, ge=0.0, description="Entropy taker 费率 bps")
    hedge_fee_bps: float = Field(default=0.0, ge=0.0, description="rblighter taker 费率 bps")
    leg_slippage_bps: float = Field(default=30.0, ge=0.0, description="Entropy 腿滑点 bps")
    hedge_slippage_bps: float = Field(default=15.0, ge=0.0, description="对冲腿滑点 bps")
    humanize: bool = Field(default=True, description="是否启用 run.sh 人类化随机抖动")
    auto_start: bool = Field(default=False, description="创建后立即启动（不推荐首次使用）")


@api.get("/tasks")
def api_tasks() -> Dict[str, Any]:
    """列出所有保存的任务。"""
    tasks = _load_tasks()
    for t in tasks:
        t["runtime_status"] = _task_status(t)
    return {"tasks": tasks, "time": _now_bj()}


@api.get("/tasks/{task_id}")
def api_task(task_id: str) -> Dict[str, Any]:
    task = _find_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    task["runtime_status"] = _task_status(task)
    return task


@api.post("/tasks")
def api_create_task(req: TaskCreate) -> Dict[str, Any]:
    """
    创建任务：
      1) 把任务参数写入 config.yaml；
      2) 持久化任务元数据；
      3) 若 auto_start=True 则立即启动（需满足 live_start_samples 样本要求）。
    """
    task = req.dict()
    symbol = task["symbol"].strip().upper()
    task["symbol"] = symbol
    task["id"] = _gen_task_id(symbol)
    task["name"] = (task.get("name") or f"{symbol} 套利").strip() or f"{symbol} 套利"
    task["created_at"] = _now_bj()
    task["status"] = "idle"

    cfg = _load_config()
    _apply_task_to_config(task, cfg)
    _save_config(cfg)

    tasks = _load_tasks()
    tasks.insert(0, task)
    _save_tasks(tasks)

    result = {"ok": True, "task": task, "config_path": str(_resolve_cfg()), "time": _now_bj()}

    if task.get("auto_start"):
        start_res = _start_task(task)
        result["auto_start"] = start_res

    return result


@api.post("/tasks/{task_id}/start")
def api_start_task(task_id: str, force_record: bool = Query(default=False)) -> Dict[str, Any]:
    """启动指定任务：先写 config，再运行 run.sh --daemon。"""
    task = _find_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    samples = _count_csv(LOG_DIR / "minutes.csv")
    min_samples = int(task.get("live_start_samples", 0) or 0)
    if not force_record and samples < min_samples:
        return {
            "ok": False,
            "error": "样本不足",
            "samples": samples,
            "required": min_samples,
            "message": f"当前 minutes.csv 样本 {samples}，低于任务要求 {min_samples}。请先用「采集」跑够样本，或勾选「强制采集模式启动」。",
            "needs_record": True,
            "time": _now_bj(),
        }

    return _start_task(task, force_record=force_record)


def _start_task(task: Dict[str, Any], force_record: bool = False) -> Dict[str, Any]:
    """内部启动逻辑。"""
    symbol = task.get("symbol", "SNDK")
    cfg = _load_config()
    _apply_task_to_config(task, cfg)
    _save_config(cfg)

    run_sh = REPO_DIR / "run.sh"
    if not run_sh.exists():
        raise HTTPException(500, f"run.sh 不存在：{run_sh}")

    env = {}
    if not task.get("humanize", True):
        env["HUMANIZE"] = "0"

    if force_record:
        # 强制采集模式：直接跑 collect.sh
        collect_sh = REPO_DIR / "collect.sh"
        if not collect_sh.exists():
            raise HTTPException(500, f"collect.sh 不存在：{collect_sh}")
        proc = _run(f"bash {collect_sh} {symbol}", env=env)
        task["status"] = "record"
        _update_task_status(task["id"], "record")
        return {
            "ok": True, "mode": "record", "symbol": symbol,
            "stdout": proc.stdout, "stderr": proc.stderr, "time": _now_bj(),
        }

    proc = _run(f"bash {run_sh} --daemon {symbol}", env=env)
    task["status"] = "running"
    _update_task_status(task["id"], "running")
    return {
        "ok": True, "mode": "smart", "symbol": symbol, "humanize": task.get("humanize", True),
        "stdout": proc.stdout, "stderr": proc.stderr, "time": _now_bj(),
    }


def _update_task_status(task_id: str, status: str) -> None:
    tasks = _load_tasks()
    for t in tasks:
        if t.get("id") == task_id:
            t["status"] = status
            t["updated_at"] = _now_bj()
            break
    _save_tasks(tasks)


@api.post("/tasks/{task_id}/stop")
def api_stop_task(task_id: str) -> Dict[str, Any]:
    """停止指定任务对应 symbol 的进程。"""
    task = _find_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    symbol = task.get("symbol", "SNDK")
    _run(f"pkill -f 'run.sh.*{symbol}' 2>/dev/null || true")
    _run(f"pkill -f 'run.sh {symbol}' 2>/dev/null || true")
    _run(f"pkill -f 'main.py --symbol {symbol}' 2>/dev/null || true")
    _run(f"pkill -f 'main.py --record-only --symbol {symbol}' 2>/dev/null || true")
    _update_task_status(task_id, "idle")
    return {"ok": True, "symbol": symbol, "time": _now_bj()}


@api.delete("/tasks/{task_id}")
def api_delete_task(task_id: str) -> Dict[str, Any]:
    """删除任务（不会停止进程，需先点停止）。"""
    tasks = _load_tasks()
    new_tasks = [t for t in tasks if t.get("id") != task_id]
    if len(new_tasks) == len(tasks):
        raise HTTPException(404, "任务不存在")
    _save_tasks(new_tasks)
    return {"ok": True, "deleted": task_id, "time": _now_bj()}


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
# 控制（保留，兼容旧版直接控制）
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


# =============================================================================
# 账户余额 / 持仓查询（只读，不交易）
# 读取与引擎共用的 .env（HL_* / LIGHTER_*），分别查两边真实账户状态。
# 任一边失败不影响另一边显示；返回中明确 ok / error 字段。
# =============================================================================
def _load_dotenv() -> Dict[str, str]:
    """读取仓库根目录 .env（与引擎共用），不依赖额外库。"""
    env: Dict[str, str] = {}
    p = REPO_DIR / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _query_hyperliquid(env: Dict[str, str]) -> Dict[str, Any]:
    """只读查询 Hyperliquid 账户权益 / 可用 / 持仓。"""
    addr = (env.get("HL_ACCOUNT_ADDRESS") or "").strip()
    if not addr:
        return {"ok": False, "error": "未配置 HL_ACCOUNT_ADDRESS"}
    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        state = info.user_state(addr)
        ms = state.get("marginSummary", {}) or {}
        positions = []
        for p in state.get("assetPositions", []) or []:
            pos = (p.get("position") or {}) if isinstance(p, dict) else {}
            try:
                szi = float(pos.get("szi", 0) or 0)
            except Exception:
                szi = 0
            if szi != 0:
                positions.append({
                    "symbol": pos.get("coin"),
                    "size": szi,
                    "side": "long" if szi > 0 else "short",
                    "unrealized_pnl": _to_float(pos.get("unrealizedPnl")),
                })
        return {
            "ok": True,
            "equity": _to_float(ms.get("accountValue")),
            "available": _to_float(state.get("withdrawable")),
            "positions": positions,
        }
    except Exception as e:
        return {"ok": False, "error": f"Hyperliquid 查询失败：{e}"}


async def _query_lighter_async(env: Dict[str, str]) -> Dict[str, Any]:
    """只读查询 rblighter（Lighter Robinhood 链）账户。使用 lighter-python SDK。"""
    idx = (env.get("LIGHTER_ACCOUNT_INDEX") or "").strip()
    pk = (env.get("LIGHTER_API_PRIVATE_KEY") or "").strip()
    if not (idx and pk):
        return {"ok": False, "error": "未配置 LIGHTER_ACCOUNT_INDEX / LIGHTER_API_PRIVATE_KEY"}
    client = None
    try:
        import lighter
        from lighter import AccountApi
        # 切到 Robinhood 链部署（与 TS SDK 的 LIGHTER_NETWORK 一致）。
        net = (env.get("LIGHTER_NETWORK") or "robinhood").strip()
        os.environ["LIGHTER_NETWORK"] = net
        client = lighter.ApiClient()
        account_api = AccountApi(client)
        account = await account_api.account(by="index", value=str(idx))
        if hasattr(account, "to_dict"):
            acc = account.to_dict()
        elif isinstance(account, dict):
            acc = account
        else:
            acc = {}
        positions = []
        for pos in (acc.get("positions") or []):
            sym = pos.get("symbol")
            try:
                sign = int(pos.get("sign", 0) or 0)
            except Exception:
                sign = 0
            try:
                size = float(pos.get("position", 0) or 0)
            except Exception:
                size = 0
            if size != 0:
                positions.append({
                    "symbol": sym,
                    "size": size * sign,
                    "side": "long" if sign > 0 else "short",
                    "unrealized_pnl": _to_float(pos.get("unrealized_pnl")),
                })
        return {
            "ok": True,
            # rblighter 用 USDG 结算，collateral 即总权益
            "equity": _to_float(acc.get("collateral")),
            "available": _to_float(acc.get("available_balance")),
            "positions": positions,
        }
    except Exception as e:
        return {"ok": False, "error": f"rblighter 查询失败：{e}"}
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


def _query_lighter(env: Dict[str, str]) -> Dict[str, Any]:
    try:
        return asyncio.run(_query_lighter_async(env))
    except Exception as e:
        return {"ok": False, "error": f"rblighter 查询失败：{e}"}


def _compute_net_exposure(hl: Dict[str, Any], rb: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按 symbol 配对两边持仓，计算净敞口（对冲方向应相反，理想为 0）。"""
    try:
        hpos = {p["symbol"]: p["size"] for p in (hl.get("positions") or []) if p.get("symbol")}
        rpos = {p["symbol"]: p["size"] for p in (rb.get("positions") or []) if p.get("symbol")}
        syms = set(hpos) | set(rpos)
        return [{
            "symbol": s,
            "hyperliquid": hpos.get(s, 0.0),
            "rblighter": rpos.get(s, 0.0),
            "net": hpos.get(s, 0.0) + rpos.get(s, 0.0),
        } for s in sorted(syms)]
    except Exception:
        return []


@api.get("/account")
def api_account() -> Dict[str, Any]:
    """只读查询两边真实账户（权益 / 可用 / 持仓）与净敞口。不交易。"""
    env = _load_dotenv()
    hl = _query_hyperliquid(env)
    rb = _query_lighter(env)
    return {
        "hyperliquid": hl,
        "rblighter": rb,
        "net_exposure": _compute_net_exposure(hl, rb),
        "time": _now_bj(),
    }


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
