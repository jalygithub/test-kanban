"""环境管理三件套（参考 vibe-astock 的 setup / doctor / start）。

用法：
    py -3 scripts/manage.py setup    # 建虚拟环境 + 装依赖
    py -3 scripts/manage.py doctor   # 体检：Python/依赖/网络/数据库
    py -3 scripts/manage.py start    # 启动 FastAPI
    py -3 scripts/manage.py fetch    # 手动抓一次数据
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
VENV = BACKEND / ".venv"
VENV_PY = VENV / ("Scripts" if os.name == "nt" else "bin") / (
    "python.exe" if os.name == "nt" else "python"
)
REQUIREMENTS = BACKEND / "requirements.txt"

GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}OK{RESET}    {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}WARN{RESET}  {msg}")


def python() -> str:
    return str(VENV_PY if VENV_PY.exists() else Path(sys.executable))


def cmd_setup() -> int:
    print("== setup ==")
    if not VENV_PY.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        ok(f"虚拟环境已创建 {VENV}")
    else:
        ok("虚拟环境已存在")
    subprocess.run([python(), "-m", "pip", "install", "-q", "-U", "pip"], check=False)
    subprocess.run([python(), "-m", "pip", "install", "-q", "-r",
                    str(REQUIREMENTS)], check=True)
    ok("依赖安装完成")
    return 0


def cmd_doctor() -> int:
    print("== doctor ==")
    failures = 0

    ver = sys.version.split()[0]
    if sys.version_info >= (3, 11):
        ok(f"Python {ver}")
    else:
        bad(f"Python {ver}，需要 >= 3.11")
        failures += 1

    try:
        import akshare
        ok(f"akshare {akshare.__version__}")
    except Exception as exc:  # noqa: BLE001
        bad(f"akshare 不可用: {exc}")
        failures += 1

    try:
        import fastapi  # noqa: F401
        import pandas  # noqa: F401
        ok("fastapi / pandas 已安装")
    except Exception as exc:  # noqa: BLE001
        bad(f"依赖缺失: {exc}（先跑 setup）")
        failures += 1

    # 网络体检：IPv4 优先修复是否生效 + 东财/同花顺连通性
    sys.path.insert(0, str(BACKEND))
    from app import netfix  # noqa: E402

    netfix.install(min_interval=0.0)
    import socket
    res = socket.getaddrinfo("push2.eastmoney.com", 443,
                             proto=socket.IPPROTO_TCP)
    if res and res[0][0] == socket.AF_INET:
        ok("IPv4 优先解析已生效")
    else:
        warn("IPv4 优先未生效（push2 可能走 IPv6 坏节点）")

    import requests
    for name, url in [
        ("同花顺资金流", "http://data.10jqka.com.cn/funds/gnzjl/"),
        ("东财 push2ex", "https://push2ex.eastmoney.com/"),
    ]:
        try:
            r = requests.get(url, timeout=8, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "Chrome/120.0 Safari/537.36"})
            if r.status_code < 500:
                ok(f"{name} 连通 ({r.status_code})")
            else:
                warn(f"{name} HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            warn(f"{name} 不可达: {type(exc).__name__}（可能被限流，稍后重试）")

    try:
        from app.store import db
        db.init_db()
        ok(f"数据库可写 {db.settings.db_path}")
    except Exception as exc:  # noqa: BLE001
        bad(f"数据库失败: {exc}")
        failures += 1

    print()
    if failures:
        print(f"{RED}体检未通过：{failures} 项失败{RESET}")
        return 1
    print(f"{GREEN}体检通过{RESET}")
    return 0


def cmd_start() -> int:
    print("== start ==")
    os.chdir(BACKEND)
    env = {**os.environ, "PYTHONPATH": str(BACKEND),
           "PYTHONIOENCODING": "utf-8"}
    # 必须显式指定 reload-dir：默认只监视 cwd，跨 PYTHONPATH 的包路径会漏掉
    os.execvpe(python(), [python(), "-m", "uvicorn", "app.main:app",
                          "--host", "127.0.0.1", "--port", "8000",
                          "--reload", "--reload-dir", str(BACKEND / "app")], env)


def cmd_fetch() -> int:
    print("== fetch ==")
    sys.path.insert(0, str(BACKEND))
    from app.api.routes import refresh  # noqa: E402
    print(refresh())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="股票看板环境管理")
    parser.add_argument("cmd", choices=["setup", "doctor", "start", "fetch"])
    args = parser.parse_args()
    return {"setup": cmd_setup, "doctor": cmd_doctor,
            "start": cmd_start, "fetch": cmd_fetch}[args.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
