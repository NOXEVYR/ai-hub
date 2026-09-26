"""Windowless local launcher. It reuses an existing AI Hub instance."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def health(port):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            data = json.load(response)
        return data if data.get("app") == "ai-hub" else None
    except (OSError, ValueError):
        return None


def port_busy(port):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def acquire_mutex(port):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, True, f"Local\\AIHub-launch-{port}")
    if not handle:
        raise OSError("无法创建启动锁")
    return kernel, handle, ctypes.get_last_error() != 183


def ensure_running(port, timeout=20, startup_state=None):
    if health(port):
        return {"status": "reused", "port": port}
    kernel, handle, owner = acquire_mutex(port)
    try:
        if not owner:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if health(port):
                    return {"status": "reused", "port": port}
                time.sleep(0.2)
            raise RuntimeError("另一个启动过程尚未完成，请稍后重新打开曜核。")
        if health(port):
            return {"status": "reused", "port": port}
        if port_busy(port):
            raise RuntimeError(f"端口 {port} 已被其他服务或旧版曜核（AI Hub）占用。请关闭该服务后重试，或调整 data/config.json 中的端口。")
        DATA.mkdir(exist_ok=True)
        interpreter = Path(sys.executable)
        if interpreter.name.lower() == "pythonw.exe" and interpreter.with_name("python.exe").is_file():
            interpreter = interpreter.with_name("python.exe")
        with (DATA / "server.log").open("ab") as log:
            process = subprocess.Popen([str(interpreter), "-u", str(ROOT / "server.py"), "serve", "--port", str(port)],
                                       cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if startup_state is not None:
            startup_state["process"] = process
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if health(port):
                (DATA / "server.pid.json").write_text(json.dumps({"pid": process.pid, "port": port, "root": str(ROOT)}, indent=2), encoding="utf-8")
                return {"status": "started", "port": port, "pid": process.pid}
            if process.poll() is not None:
                raise RuntimeError("曜核未能启动。详细原因已写入 data/server.log。")
            time.sleep(0.2)
        raise RuntimeError("曜核启动超时。请查看 data/server.log，稍后重新打开会复用已启动的服务。")
    finally:
        if owner:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def write_desktop_result(request_id, status, startup_state):
    """A unique, non-overwriting receipt lets the desktop distinguish a late child.

    No token or arbitrary output path is accepted. Failure to write this receipt
    leaves the desktop in its conservative unknown-startup state.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("桌面启动标识格式有误")
    folder = DATA / "desktop"
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink() or folder.resolve().parent != DATA.resolve():
        raise ValueError("桌面启动结果目录不可重定向")
    child = startup_state.get("process")
    alive = child is not None and child.poll() is None
    receipt = {"schema": "ai-hub-desktop-start-v1", "request_id": request_id,
               "install_root": str(ROOT.resolve()), "status": status, "child_alive": alive}
    if alive:
        # Hold/read the handle of the child we actually launched. A later desktop
        # check compares creation time as well as PID, so PID reuse has no authority.
        try:
            creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            get_times = ctypes.windll.kernel32.GetProcessTimes
            get_times.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                 ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
            if get_times(int(child._handle), ctypes.byref(creation), ctypes.byref(exit_time),
                         ctypes.byref(kernel_time), ctypes.byref(user_time)):
                receipt["child_pid"] = child.pid
                receipt["child_start_filetime"] = str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    with (folder / ("startup-" + request_id + ".json")).open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-dialog", action="store_true")
    parser.add_argument("--desktop-result-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.desktop_result_id and not re.fullmatch(r"[0-9a-f]{32}", args.desktop_result_id):
        parser.error("桌面启动标识格式有误")
    startup_state = {}
    outcome = "failed"
    try:
        from aihub.app_update import startup_guard
        startup_guard(ROOT)
        settings = json.loads((DATA / "config.json").read_text(encoding="utf-8-sig")) if (DATA / "config.json").is_file() else {}
        port = args.port or settings.get("server", {}).get("port", 8765)
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("端口必须是1024至65535之间的整数。")
        result = ensure_running(port, startup_state=startup_state)
        outcome = result["status"]
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{port}/#/overview", new=0)
        if sys.stdout:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        DATA.mkdir(exist_ok=True)
        message = str(error)
        with (DATA / "launcher.log").open("a", encoding="utf-8") as log:
            log.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
        if not args.no_dialog:
            ctypes.windll.user32.MessageBoxW(None, message, "曜核 · 启动提示", 0x10)
        return 1
    finally:
        if args.desktop_result_id:
            try:
                write_desktop_result(args.desktop_result_id, outcome, startup_state)
            except (OSError, ValueError):
                # The desktop deliberately does not treat a missing receipt as an
                # absent child; never replace an existing file to force success.
                pass


if __name__ == "__main__":
    raise SystemExit(main())
