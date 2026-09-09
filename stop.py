"""通过本机认证接口停止查看器，不按名称结束其他 Python 进程。"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from vault import protect


def main():
    """读取本工具会话并请求正常停止，网络或解密失败时不强制终止进程。"""
    try:
        runtime = Path(os.environ["LOCALAPPDATA"]) / "CodexQuotaViewer" / "runtime.dpapi"
        info = json.loads(protect(runtime.read_bytes(), decrypt=True))
        port = info["port"]
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}",
                                     headers={"X-Viewer-Token": info["token"], "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=3):
            print("查看器已停止。")
    except urllib.error.HTTPError:
        print("暂时无法停止，请等待当前查询或登录结束后重试。")
        raise SystemExit(1)
    except Exception:
        print("查看器未运行或会话文件不可用。")


if __name__ == "__main__":
    main()
