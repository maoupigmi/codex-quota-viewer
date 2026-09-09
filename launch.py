"""双击启动入口：复用本机实例并打开已认证页面，后台进程不显示命令窗口。"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from vault import protect


def open_existing(runtime):
    """解密本机会话信息并验证服务；不存在或已退出时返回 False。"""
    try:
        info = json.loads(protect(runtime.read_bytes(), decrypt=True))
        port, token = info["port"], info["token"]
        if not isinstance(port, int) or not 1 <= port <= 65535 or not isinstance(token, str):
            return False
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/accounts", headers={"X-Viewer-Token": token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=2) as response:
            if response.status != 200:
                return False
        webbrowser.open(f"http://127.0.0.1:{port}/#token={token}")
        return True
    except Exception:
        return False


def main():
    """先验证已有实例，再启动隐藏的 Python 服务；不记录解密后的密钥。"""
    root = Path(__file__).resolve().parent
    data = Path(os.environ["LOCALAPPDATA"]) / "CodexQuotaViewer"
    runtime = data / "runtime.dpapi"
    if open_existing(runtime):
        return
    process = subprocess.Popen([sys.executable, str(root / "server.py"), "--no-browser"], cwd=root,
                               creationflags=subprocess.CREATE_NO_WINDOW,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if process.poll() is not None:
            break
        time.sleep(0.2)
        if open_existing(runtime):
            return process
    print("启动失败。可运行 python server.py 查看启动提示；请检查端口 18741 与数据目录权限。")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
