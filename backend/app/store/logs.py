"""运行期日志文件的查看与截断。

日志由 `start_all.py` 把子进程 stdout 重定向写入，**后端进程自己不持有句柄**，
但写入方（uvicorn 父进程）一直开着文件 —— Windows 下不能删、也不能改名，
只能原地截断（`truncate(0)`），文件被继续 append，句柄不失效。

路径白名单在这里收口：调用方传的是日志**名**，不是路径，
避免 `/api/data/purge` 变成任意文件删除接口。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from app.config import settings

logger = logging.getLogger(__name__)

# 允许查看/截断的日志名。改这里才会开放新文件。
LOG_FILES: Dict[str, str] = {
    "backend.log": "后端",
    "frontend.log": "前端 (Vite)",
    "start.log": "启动器",
}


def _resolve(name: str) -> Path:
    if name not in LOG_FILES:
        raise KeyError(f"不允许操作的日志：{name}（白名单：{sorted(LOG_FILES)}）")
    return settings.run_dir / name


def list_logs() -> List[Dict[str, Any]]:
    """只读：各日志的大小与最后写入时间。"""
    out: List[Dict[str, Any]] = []
    for name, label in LOG_FILES.items():
        p = settings.run_dir / name
        try:
            st = p.stat()
            out.append({"name": name, "label": label, "bytes": st.st_size,
                        "mtime": st.st_mtime, "exists": True})
        except OSError:
            out.append({"name": name, "label": label, "bytes": 0,
                        "mtime": 0.0, "exists": False})
    # 服务注册表与备份也算运行期产物
    for name in ("services.json", "services.json.bak"):
        p = settings.run_dir / name
        try:
            out.append({"name": name, "label": "服务注册表",
                        "bytes": p.stat().st_size, "mtime": p.stat().st_mtime,
                        "exists": True})
        except OSError:
            pass
    return out


def truncate_log(name: str) -> Dict[str, Any]:
    """原地截断日志到 0 字节。只接受白名单名。"""
    try:
        p = _resolve(name)
    except KeyError as exc:
        return {"error": str(exc), "applied": False}

    try:
        before = p.stat().st_size
    except OSError:
        return {"error": f"{name} 不存在", "applied": False}

    try:
        # 'r+b' 而非 'w'：'w' 会先删内容并要求独占，写入方还开着就会 EACCES。
        # 先打开再 truncate，写入方的句柄与偏移都不受影响。
        with open(p, "r+b") as fh:
            fh.truncate(0)
    except OSError as exc:
        logger.warning("截断 %s 失败：%s", name, exc)
        return {"error": f"截断失败：{exc}", "before": before, "applied": False}

    logger.info("已截断日志 %s：%d -> 0 bytes", name, before)
    return {"name": name, "before": before, "after": 0, "applied": True}
