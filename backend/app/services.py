"""本机服务进程助手：给页面上的「停止服务」按钮用。

前端 dev server（5173）与后端 uvicorn（8000）是两个进程，点一次要一次关干净。
启动器 ``scripts/start_all.py`` 把 PID 记在 ``run/services.json``，这里优先读它
——快，也不会误伤跑在同一端口上的别的东西；文件没有或记录过期，再退回按端口查。

查 PID 与结束进程都走系统命令（PowerShell / tasklist / taskkill），只在停止时调，
慢一秒无所谓，稳比快重要。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# backend/app/services.py → parents[2] = 仓库根
ROOT = Path(__file__).resolve().parents[2]
SERVICES_FILE = ROOT / "run" / "services.json"
FRONTEND_PORT = 5173


def _recorded_pid(name: str) -> Optional[int]:
    """启动器写下的 pid；没有文件、字段缺失或不是数字都返回 None。"""
    try:
        data = json.loads(SERVICES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    node = data.get(name)
    pid = node.get("pid") if isinstance(node, dict) else None
    return pid if isinstance(pid, int) and pid > 0 else None


def _pid_on_port(port: int) -> Optional[int]:
    """按端口反查监听进程 PID。"""
    script = (
        "(Get-NetTCPConnection -State Listen -LocalPort {0} "
        "-ErrorAction SilentlyContinue | Select-Object -First 1 "
        "-ExpandProperty OwningProcess)"
    ).format(port)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 查不到就算了，交给调用方兜底
        logger.warning("查端口 %s 的 PID 失败: %s", port, exc)
        return None
    txt = (out.stdout or b"").decode("utf-8", errors="replace").strip()
    first = txt.splitlines()[0].strip() if txt else ""
    return int(first) if first.isdigit() else None


def _process_name(pid: int) -> str:
    """进程名；进程不在了返回空串。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=20,
        )
    except Exception:  # noqa: BLE001
        return ""
    line = (out.stdout or b"").decode("utf-8", errors="replace").strip()
    if not line or line.startswith("INFO"):
        return ""
    return line.split(",")[0].strip('"')


def _kill(pid: int) -> bool:
    try:
        out = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                             capture_output=True, timeout=20)
        return out.returncode == 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("taskkill %s 失败: %s", pid, exc)
        return False


def stop_frontend() -> Dict[str, str]:
    """结束前端 dev server，返回一份给人看的结果描述。"""
    pid = _recorded_pid("frontend")
    name = _process_name(pid) if pid else ""

    if not pid or not name:
        # 没记录（手动起的服务）或记录过期 → 按端口现查一次
        pid = _pid_on_port(FRONTEND_PORT)
        name = _process_name(pid) if pid else ""

    if not pid or not name:
        return {"frontend": "未在运行"}

    if not name.lower().startswith("node"):
        # 端口被别的东西占着，宁可不动手
        return {"frontend": f"{FRONTEND_PORT} 端口被 {name}（pid={pid}）占用，未处理"}

    if pid == os.getpid():  # pragma: no cover 防呆
        return {"frontend": "pid 就是本进程，未处理"}

    if not _kill(pid):
        return {"frontend": f"结束失败（{name} pid={pid}）"}

    logger.info("前端 dev server 已停止 pid=%s (%s)", pid, name)
    return {"frontend": f"已停止（{name} pid={pid}）"}
