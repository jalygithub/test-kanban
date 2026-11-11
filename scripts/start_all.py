"""一键启动 / 停止：后端 127.0.0.1:8000 + 前端 localhost:5173，全程静默。

用法::

    py -3 scripts\\start_all.py            启动（已在跑的跳过）并打开浏览器
    py -3 scripts\\start_all.py stop       停掉两个服务
    py -3 scripts\\start_all.py status     看两个端口谁在跑
    py -3 scripts\\start_all.py shortcut   在桌面生成「A股看板一键启动」快捷方式

桌面图标调的是 ``pythonw.exe``（没有控制台），所以本文件守三条约定：

1. 输出写 ``run\\start.log``，打印只在 stdout 可用时做，绝不依赖控制台；
2. 子进程用 ``CREATE_NO_WINDOW`` 起：不共享控制台、不随启动器退出被带走，
   而且**第二层**（venv python 再拉的真解释器）会继承这个「没有窗口的控制台」，
   不会像 ``DETACHED_PROCESS`` 那样被 Windows 补开一个终端窗口；
3. 失败弹 Windows 消息框（没控制台可看），成功直接开浏览器。

停止有两条路：页面左侧菜单最下方的「停止服务」按钮（POST /api/system/stop，
由后端关掉前端再退出），以及这里的 ``stop`` —— 后者是同一件事的命令行版。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
MANAGE = BACKEND / "scripts" / "manage.py"
VENV_SCRIPTS = BACKEND / ".venv" / "Scripts"
VENV_PY = VENV_SCRIPTS / "python.exe"
VENV_PYW = VENV_SCRIPTS / "pythonw.exe"

RUN = ROOT / "run"
START_LOG = RUN / "start.log"
BACKEND_LOG = RUN / "backend.log"
FRONTEND_LOG = RUN / "frontend.log"
SERVICES_FILE = RUN / "services.json"

BACKEND_PORT = 8000
FRONTEND_PORT = 5173
HEALTH_URL = f"http://127.0.0.1:{BACKEND_PORT}/api/health"
SITE_URL = f"http://localhost:{FRONTEND_PORT}"

SHORTCUT_NAME = "A股看板一键启动.lnk"

# CREATE_NO_WINDOW：子进程不弹窗，也不受启动器生命周期约束。
#
# 这里**不能用 DETACHED_PROCESS** —— 后端是「venv python → 它再拉一个真解释器」
# 的两层结构，第二层是普通 CreateProcess：父进程没有控制台时，Windows 会**新起
# 一个控制台**（Win11 默认终端是 Windows Terminal，于是桌面上真弹一个窗口），
# 「完全静默」当场破功。CREATE_NO_WINDOW 让父进程带一个「没有窗口的控制台」，
# 第二层继承它，也就不再新开窗口；对启动器里的 powershell / tasklist 同理。
NO_WINDOW = 0x08000000


# ------------------------------------------------------------------ 基础件


def log(msg: str) -> None:
    """落一份到 run/start.log；有控制台就顺带打印（pythonw 下 stdout 可能为 None）。"""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        RUN.mkdir(exist_ok=True)
        with START_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except Exception:  # noqa: BLE001  pythonw 的 stdout 是坏句柄，直接忽略
        pass


def ps_quote(text: str) -> str:
    """PowerShell 单引号字面量：内部单引号翻倍即可，换行原样保留。"""
    return "'" + text.replace("'", "''") + "'"


def run_ps(script: str, timeout: int = 60) -> str:
    """跑一段 PowerShell。用 -EncodedCommand，避免中文/引号被控制台编码搞坏。"""
    b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    out = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", b64],
        capture_output=True, timeout=timeout,
        creationflags=NO_WINDOW,
    )
    return (out.stdout or b"").decode("utf-8", errors="replace").strip()


def alert(title: str, text: str) -> None:
    """没控制台时唯一的反馈手段：弹一个系统消息框。"""
    try:
        run_ps("Add-Type -AssemblyName System.Windows.Forms; "
               "[System.Windows.Forms.MessageBox]::Show("
               f"{ps_quote(text)}, {ps_quote(title)}, 'OK', 'Error') | Out-Null")
    except Exception:  # noqa: BLE001 弹窗失败不值得让启动流程崩掉
        log("消息框弹出失败")


def has_console() -> bool:
    try:
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001
        return False


def tail_of(path: Path, n: int = 15) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"（{path} 读不到）"
    if not lines:
        return f"（{path} 是空的）"
    return "\n".join([f"── {path.name} 最后 {n} 行 ──"] + lines[-n:])


# ------------------------------------------------------------------ 端口/进程


def port_open(port: int) -> bool:
    """端口通不通。

    必须用 ``localhost`` 而不是写死 ``127.0.0.1``：vite 只监听 ``::1``，
    只探 IPv4 会把「已经在跑」误判成「没起」，于是又拉一个、白白撞端口。
    ``create_connection`` 会把 localhost 解析出的 ::1 / 127.0.0.1 都试一遍。
    """
    try:
        with socket.create_connection(("localhost", port), timeout=0.4):
            return True
    except OSError:
        return False


def pid_on_port(port: int) -> Optional[int]:
    script = (
        "(Get-NetTCPConnection -State Listen -LocalPort {0} "
        "-ErrorAction SilentlyContinue | Select-Object -First 1 "
        "-ExpandProperty OwningProcess)"
    ).format(port)
    try:
        txt = run_ps(script, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    first = txt.splitlines()[0].strip() if txt else ""
    return int(first) if first.isdigit() else None


def process_name(pid: int) -> str:
    """进程名，进程不在了返回空串。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=30, creationflags=NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        return ""
    line = (out.stdout or b"").decode("utf-8", errors="replace").strip()
    if not line or line.startswith("INFO"):
        return ""
    return line.split(",")[0].strip('"')


def kill_pid(pid: int) -> bool:
    try:
        out = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                             capture_output=True, timeout=30,
                             creationflags=NO_WINDOW)
        return out.returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ 启动


def ensure_env() -> Optional[str]:
    """虚拟环境在就直接用；不在就先跑一遍 manage.py setup。"""
    if VENV_PY.exists():
        return str(VENV_PY)
    log("虚拟环境不存在，先跑 scripts/manage.py setup（首次会久一些）…")
    argv = [sys.executable, str(MANAGE), "setup"]
    try:
        if has_console():
            # 有控制台就继承窗口，让用户看得见装依赖的进度（不会新开黑框）
            code = subprocess.run(argv, cwd=str(BACKEND), timeout=1800).returncode
        else:
            # pythonw：不新建窗口，输出转存 run/setup.log，免得失败时无处可查
            out = subprocess.run(argv, cwd=str(BACKEND), timeout=1800,
                                 capture_output=True, creationflags=NO_WINDOW)
            text = ((out.stdout or b"") + b"\n" + (out.stderr or b"")).decode(
                "utf-8", errors="replace")
            RUN.mkdir(exist_ok=True)
            (RUN / "setup.log").write_text(text, encoding="utf-8")
            code = out.returncode
    except Exception as exc:  # noqa: BLE001
        log(f"setup 失败: {exc}")
        return None
    if code != 0:
        log(f"setup 退出码 {code}，输出见 run/setup.log")
        return None
    return str(VENV_PY) if VENV_PY.exists() else None


def log_max_bytes() -> int:
    """启动轮转阈值（字节）。

    与 ``backend/app/config.py`` 的 ``log_max_bytes`` 同名同默认值，但启动器是
    独立进程、读不到 pydantic 的配置对象，所以这里自己取：环境变量优先，其次
    ``backend/.env``，都没有就用默认 20MB。改一处默认值要同步改两处。
    """
    default = 20 * 1024 * 1024
    key = "STOCK_LOG_MAX_BYTES"
    if key in os.environ:
        try:
            return max(0, int(os.environ[key]))
        except ValueError:
            return default
    try:
        for line in (BACKEND / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key):
                return max(0, int(line.split("=", 1)[1].strip()))
    except (OSError, ValueError):
        pass
    return default


def rotate_log(path: Path, max_bytes: int) -> None:
    """超过阈值就把当前日志挪成 ``.1``（保留一份旧的），腾出一个新文件。

    **只在启动前调用**：那时上一版服务已停、写入方没起，文件无人占用，Windows
    上才允许改名。跑动中改名会撞共享冲突 —— 页面里的「清空日志」走的是另一条
    路（原地 ``truncate(0)``，见 backend/app/store/logs.py）。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    old = path.with_name(path.name + ".1")
    try:
        if old.exists():
            old.unlink()
        path.rename(old)
        log(f"日志轮转：{path.name} {size / 1024 / 1024:.1f} MB -> {old.name}")
    except OSError as exc:
        log(f"日志轮转失败 {path.name}：{exc}")


def start_detached(argv: List[str], cwd: Path, log_file: Path,
                   env: Optional[Dict[str, str]] = None) -> int:
    """起一个不弹窗的进程，stdout/stderr 全进日志文件。

    旗标用 ``CREATE_NO_WINDOW`` 而不是 ``DETACHED_PROCESS``：后者只护得住第一
    层，venv python 再拉的第二层会被 Windows 补开终端（见文件头约定 2）。
    """
    RUN.mkdir(exist_ok=True)
    with log_file.open("ab", buffering=0) as fh:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env,
            stdin=subprocess.DEVNULL,
            stdout=fh, stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
        )
    return proc.pid


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return 200 <= res.status < 300
    except Exception:  # noqa: BLE001
        return False


def wait_for(label: str, check: Callable[[], bool], timeout: float,
             pid: Optional[int] = None, log_file: Optional[Path] = None) -> bool:
    deadline = time.monotonic() + timeout
    n = 0
    while time.monotonic() < deadline:
        if check():
            log(f"{label} 就绪")
            return True
        n += 1
        # 进程挂了就别干等到超时：每 3 秒探一次 tasklist
        if pid and n % 6 == 0 and process_name(pid) == "":
            log(f"{label} 对应进程 pid={pid} 已退出")
            if log_file:
                log(tail_of(log_file, 12))
            return False
        time.sleep(0.5)
    log(f"{label} 等待超时（{int(timeout)}s）")
    if log_file:
        log(tail_of(log_file, 12))
    return False


def _fail(msg: str, log_files: List[Path]) -> int:
    log(f"启动失败：{msg}")
    detail = "\n\n".join(tail_of(p, 15) for p in log_files)
    text = f"{msg}\n\n{detail}" if detail else msg
    log(detail)
    if not has_console():
        alert("看板启动失败", text)
    return 1


def cmd_start(open_browser: bool = True) -> int:
    log("== 一键启动 ==")
    started: Dict[str, Dict[str, object]] = {}
    backend_pid: Optional[int] = None
    frontend_pid: Optional[int] = None

    # ---- 后端
    if port_open(BACKEND_PORT):
        backend_pid = pid_on_port(BACKEND_PORT)
        log(f"后端已在运行（{BACKEND_PORT}，pid={backend_pid}），跳过")
        started["backend"] = {"pid": backend_pid, "skipped": True}
    else:
        py = ensure_env()
        if not py:
            return _fail("虚拟环境创建失败，请手动跑："
                         "py -3 backend/scripts/manage.py setup", [START_LOG])
        rotate_log(BACKEND_LOG, log_max_bytes())
        backend_pid = start_detached(
            [py, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
            BACKEND, BACKEND_LOG,
            {**os.environ, "PYTHONPATH": str(BACKEND),
             "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
        log(f"后端已启动 pid={backend_pid}，日志 {BACKEND_LOG}")
        started["backend"] = {"pid": backend_pid, "log": str(BACKEND_LOG)}

    # ---- 前端
    if port_open(FRONTEND_PORT):
        frontend_pid = pid_on_port(FRONTEND_PORT)
        log(f"前端已在运行（{FRONTEND_PORT}，pid={frontend_pid}），跳过")
        started["frontend"] = {"pid": frontend_pid, "skipped": True}
    else:
        node = shutil.which("node")
        vite = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
        if not node or not vite.exists():
            return _fail(
                f"前端起不来：node={'有' if node else '没找到'}，"
                f"vite={'有' if vite.exists() else '没装'}。"
                "先在 frontend 目录跑 npm install",
                [START_LOG],
            )
        rotate_log(FRONTEND_LOG, log_max_bytes())
        frontend_pid = start_detached(
            [node, str(vite), "--strictPort"], FRONTEND, FRONTEND_LOG,
            dict(os.environ),
        )
        log(f"前端已启动 pid={frontend_pid}，日志 {FRONTEND_LOG}")
        started["frontend"] = {"pid": frontend_pid, "log": str(FRONTEND_LOG)}

    RUN.mkdir(exist_ok=True)
    SERVICES_FILE.write_text(
        json.dumps({"started_at": datetime.now().isoformat(timespec="seconds"),
                    "backend_port": BACKEND_PORT,
                    "frontend_port": FRONTEND_PORT, **started},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ---- 就绪检查
    ok_back = wait_for("后端 /api/health", lambda: http_ok(HEALTH_URL), 60,
                       backend_pid, BACKEND_LOG)
    ok_front = wait_for("前端 5173", lambda: http_ok(SITE_URL), 45,
                        frontend_pid, FRONTEND_LOG)
    if not (ok_back and ok_front):
        files = [BACKEND_LOG] if not ok_back else []
        files += [FRONTEND_LOG] if not ok_front else []
        return _fail("服务没能在预期时间内起来", files)

    if open_browser:
        webbrowser.open(SITE_URL)
        log(f"已打开浏览器 {SITE_URL}")
    log("启动完成")
    return 0


# ------------------------------------------------------------------ 停止 / 状态


def _recorded_pid(name: str) -> Optional[int]:
    """启动器写进 run/services.json 的**树根** PID（venv python 会再拉一个解释器，
    树根 taskkill /T 能连子进程一起带走）。"""
    try:
        data = json.loads(SERVICES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    node = data.get(name)
    pid = node.get("pid") if isinstance(node, dict) else None
    return pid if isinstance(pid, int) and pid > 0 else None


def _uvicorn_pids() -> List[int]:
    """扫残留的 uvicorn：只按端口杀会漏掉 venv 拉起的父进程。"""
    script = (
        "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*-m uvicorn app.main:app*' } | "
        "Select-Object -ExpandProperty ProcessId)"
    )
    try:
        txt = run_ps(script, timeout=30)
    except Exception:  # noqa: BLE001
        return []
    return [int(x) for x in txt.split() if x.isdigit()]


def _stop_one(port: int, label: str, expect: str, record_key: str) -> int:
    """停一个服务：树根 + 端口持有者都过一遍，最后等端口真正释放。

    只看端口不够——后端是「venv python → 真解释器」两层，杀掉监听的那个
    会留下父进程；而 services.json 里的树根一杀就是整棵树。
    """
    root = _recorded_pid(record_key)
    by_port = pid_on_port(port)
    targets = [p for p in dict.fromkeys([root, by_port]) if p]

    killed = False
    for pid in targets:
        if process_name(pid).lower().startswith(expect):
            kill_pid(pid)
            killed = True

    if not killed:
        if by_port:
            # 端口被别的东西占着，宁可不动手
            log(f"{port} 被 {process_name(by_port)}（pid={by_port}）"
                f"占用，不是本项目的进程，未结束")
            return 1
        log(f"{label}未在运行（{port}）")
        return 0

    if expect == "python":
        for pid in _uvicorn_pids():
            kill_pid(pid)
            log(f"后端残留 uvicorn 已清理 pid={pid}")

    for _ in range(10):
        if not port_open(port):
            log(f"{label}已停止（{port} 已释放）")
            return 0
        time.sleep(0.5)
    log(f"{label}停止失败：{port} 仍被 pid={pid_on_port(port)} 占用")
    return 1


def cmd_stop() -> int:
    log("== 停止 ==")
    code = _stop_one(FRONTEND_PORT, "前端", "node", "frontend")
    code |= _stop_one(BACKEND_PORT, "后端", "python", "backend")
    return code


def cmd_status() -> int:
    for port, label, expect in ((BACKEND_PORT, "后端", "python"),
                                (FRONTEND_PORT, "前端", "node")):
        pid = pid_on_port(port)
        if not pid:
            print(f"{label}: 未运行（{port}）")
            continue
        name = process_name(pid)
        mark = "" if name.lower().startswith(expect) else "  ← 不是本项目进程"
        print(f"{label}: 运行中 pid={pid} {name}{mark}（{port}）")
    if SERVICES_FILE.exists():
        print(f"启动记录: {SERVICES_FILE}")
    print(f"日志目录: {RUN}")
    return 0


def cmd_shortcut() -> int:
    """在桌面生成「A股看板一键启动」快捷方式（目标 pythonw，无控制台）。"""
    desktop_txt = run_ps("[Environment]::GetFolderPath('Desktop')", timeout=30)
    if not desktop_txt:
        print("取不到桌面路径")
        return 1
    desktop = Path(desktop_txt.splitlines()[-1].strip())
    lnk = desktop / SHORTCUT_NAME

    target = VENV_PYW if VENV_PYW.exists() else Path(sys.executable)
    if not target.exists():  # pragma: no cover 兜底
        print(f"找不到可执行文件 {target}")
        return 1

    script = (
        "$sh = New-Object -ComObject WScript.Shell;"
        f"$l = $sh.CreateShortcut({ps_quote(str(lnk))});"
        f"$l.TargetPath = {ps_quote(str(target))};"
        f"$l.Arguments = {ps_quote(chr(34) + str(Path(__file__).resolve()) + chr(34))};"
        f"$l.WorkingDirectory = {ps_quote(str(ROOT))};"
        "$l.Description = '静默启动后端与前端，起好后自动打开看板';"
        "$l.Save()"
    )
    run_ps(script, timeout=30)
    if not lnk.exists():
        print(f"快捷方式没生成：{lnk}")
        return 1
    print(f"桌面快捷方式已生成：{lnk}")
    print(f"目标：{target} {Path(__file__).resolve()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="看板一键启动 / 停止")
    parser.add_argument("cmd", nargs="?", default="start",
                        choices=["start", "stop", "status", "shortcut"])
    parser.add_argument("--no-open", action="store_true",
                        help="启动后不自动打开浏览器")
    args = parser.parse_args()

    if args.cmd == "start":
        return cmd_start(open_browser=not args.no_open)
    if args.cmd == "stop":
        return cmd_stop()
    if args.cmd == "status":
        return cmd_status()
    return cmd_shortcut()


if __name__ == "__main__":
    raise SystemExit(main())
