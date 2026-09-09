"""本机配额查看器入口：浏览器界面、DPAPI 账户库和独立 OAuth 登录。"""

import argparse
import base64
import hashlib
import json
import msvcrt
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

from quota_core import (AUTH_URL, CLIENT_ID, REDIRECT_URI, TOKEN_URL, USAGE_URL,
                        CREDITS_URL, OfficialClient, QuotaError,
                        normalize_account, parse_credits, parse_quota)
from vault import Vault, protect

ROOT = Path(__file__).resolve().parent


class Application:
    """将账户、配额刷新和登录状态组合；网络层不提供模型请求能力。"""

    def __init__(self, vault, client=None):
        """初始化服务依赖与锁；client 可注入离线测试替身。"""
        self.vault = vault
        self.client = client or OfficialClient()
        self.mutation_lock = threading.RLock()
        self.login_lock = threading.Lock()
        self.login = {"status": "idle", "message": ""}
        self.login_server = None

    def refresh(self, identity):
        """串行刷新并保留失败前快照；轮换令牌后先保存，再查询额度。"""
        with self.mutation_lock:
            account = self.vault.get(identity)
            account["last_attempt"] = time.time()
            try:
                did_refresh = False
                if account.get("expires_at") and account["expires_at"] < time.time() + 30:
                    account = self.client.refresh(account)
                    self.vault.put(account)
                    did_refresh = True
                try:
                    raw = self.client.request(USAGE_URL, account["access_token"], account["account_id"])
                except QuotaError as error:
                    if error.status != 401 or did_refresh or not account["owned"]:
                        raise
                    account = self.client.refresh(account)
                    self.vault.put(account)
                    raw = self.client.request(USAGE_URL, account["access_token"], account["account_id"])
                account["quota"] = parse_quota(raw)
                account["quota_at"] = time.time()
                account["error"] = ""
            except QuotaError as error:
                account["error"] = str(error)
                account["credits_error"] = "本次额度查询失败，重置卡未刷新。"
                self.vault.put(account)
                return
            try:
                raw_credits = self.client.request(CREDITS_URL, account["access_token"], account["account_id"])
                account["credits"] = parse_credits(raw_credits)
                account["credits_at"] = time.time()
                account["credits_error"] = ""
            except QuotaError as error:
                account["credits_error"] = str(error)
            self.vault.put(account)

    def import_documents(self, documents):
        """只处理用户在界面选择的文件内容，不扫描磁盘或修改原始文件。"""
        if not isinstance(documents, list) or not 1 <= len(documents) <= 50:
            raise QuotaError("每次请选择 1 至 50 个 JSON 文件。")
        incoming = []
        for item in documents:
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                raise QuotaError("导入请求格式无效。")
            try:
                data = json.loads(item["content"].lstrip("\ufeff"))
            except ValueError:
                raise QuotaError("选中的文件不是有效 JSON；本批次未导入。") from None
            incoming.append(normalize_account(data))
        with self.mutation_lock:
            self.vault.import_accounts(incoming)
        return len({item["id"] for item in incoming})

    def begin_login(self):
        """启动单次 PKCE 流程，固定回调端口被占用时明确报错。"""
        with self.login_lock:
            if self.login_server is not None:
                raise QuotaError("已有登录正在进行，请先完成或取消。")
            verifier = secrets.token_urlsafe(48)
            state = secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            owner = self
            deadline = time.monotonic() + 300

            class Callback(BaseHTTPRequestHandler):
                """接收单次 OAuth 回调，不记录携带授权码的 URL。"""

                def log_message(self, format, *args):
                    """禁止默认访问日志泄漏授权码或 state。"""
                    pass

                def do_GET(self):
                    """验证回调路径及 state 后兑换令牌，响应只返回固定文本。"""
                    parsed = urllib.parse.urlsplit(self.path)
                    params = urllib.parse.parse_qs(parsed.query)
                    if self.headers.get("Host") not in ("localhost:1455", "127.0.0.1:1455") or parsed.path != "/auth/callback":
                        self.send_error(404)
                        return
                    if time.monotonic() > deadline or owner.login["status"] != "waiting" or not secrets.compare_digest(params.get("state", [""])[0], state):
                        self.send_error(400, "Invalid or expired login state")
                        return
                    owner.login = {"status": "exchanging", "message": "正在保存登录…"}
                    try:
                        code = params.get("code", [""])[0]
                        if not code or "error" in params:
                            raise QuotaError("登录未完成或授权被取消。")
                        result = owner.client.request(TOKEN_URL, form={"grant_type": "authorization_code", "client_id": CLIENT_ID,
                                                                      "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
                        account = normalize_account(result, owned=True)
                        with owner.mutation_lock:
                            owner.vault.import_accounts([account])
                        owner.login = {"status": "success", "message": "登录已保存，请返回配额页面点击刷新。"}
                    except QuotaError as error:
                        owner.login = {"status": "error", "message": str(error)}
                    except Exception:
                        owner.login = {"status": "error", "message": "登录保存失败，请检查本机数据目录权限后重试。"}
                    body = ("<!doctype html><meta charset=utf-8><title>配额查看器</title><p>登录流程已结束，请返回配额查看器查看结果。</p>").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy", "default-src 'none'")
                    self.end_headers()
                    self.wfile.write(body)

            try:
                callback = HTTPServer(("127.0.0.1", 1455), Callback)
            except OSError:
                raise QuotaError("登录回调端口 1455 被占用，请完成其他 Codex 登录后重试。") from None
            callback.timeout = 0.5
            self.login_server = callback
            self.login = {"status": "waiting", "message": "请在新页面登录所需账户（5 分钟内有效）。"}

            def wait_callback():
                """有期限地等待回调，流程结束即释放固定端口。"""
                try:
                    while time.monotonic() < deadline and owner.login["status"] == "waiting":
                        callback.handle_request()
                finally:
                    callback.server_close()
                    with owner.login_lock:
                        if owner.login["status"] == "waiting":
                            owner.login = {"status": "error", "message": "登录超时，请重新发起。"}
                        owner.login_server = None

            threading.Thread(target=wait_callback, daemon=True).start()
            query = urllib.parse.urlencode({"client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
                                            "scope": "openid email profile offline_access", "state": state,
                                            "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "login",
                                            "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true"})
            return AUTH_URL + "?" + query


def make_server(app, port=18741, token=None):
    """仅绑定 IPv4 回环；会话密钥、Host 和 Origin 校验保护本地管理 API。"""
    token = token or secrets.token_urlsafe(32)
    static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
              "/style.css": ("style.css", "text/css; charset=utf-8")}

    class Handler(BaseHTTPRequestHandler):
        """只暴露明确列举的本机操作，不提供任意文件读取或 URL 转发。"""

        def log_message(self, format, *args):
            """省略访问日志，避免账户信息与认证请求进入控制台。"""
            pass

        def setup(self):
            """限制单个本机连接等待时间，防止请求体长期占用线程。"""
            super().setup()
            self.connection.settimeout(10)

        def respond(self, status, content, mime="application/json; charset=utf-8"):
            """统一禁止缓存和跨站资源，正文序列化不允许非有限数。"""
            body = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def guard(self, api=False):
            """所有请求校验 Host；API 额外要求随机密钥及同源访问。"""
            expected = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            if self.headers.get("Host") != expected or (origin and origin != "http://" + expected):
                raise QuotaError("不允许跨站或非本机访问。", 403)
            if api and not secrets.compare_digest(self.headers.get("X-Viewer-Token", ""), token):
                raise QuotaError("页面会话已过期，请重新运行启动脚本。", 401)

        def do_GET(self):
            """静态文件固定映射；账户列表严格使用脱敏后的展示模型。"""
            try:
                self.guard(self.path.startswith("/api/"))
                if self.path == "/api/accounts":
                    self.respond(200, {"accounts": app.vault.public(), "login": app.login})
                elif self.path in static:
                    name, mime = static[self.path]
                    self.respond(200, (ROOT / "web" / name).read_bytes(), mime)
                else:
                    self.respond(404, {"error": "路径不存在。"})
            except QuotaError as error:
                self.respond(error.status or 400, {"error": str(error)})
            except Exception:
                self.respond(500, {"error": "本机读取失败，请检查程序文件与数据目录。"})

        def do_POST(self):
            """认证后处理导入、刷新、标签编辑、移除和登录；没有重置卡消耗操作。"""
            try:
                self.guard(api=True)
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise QuotaError("请求必须为 JSON。", 415)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4 * 1024 * 1024:
                    raise QuotaError("请求大小必须在 1 字节至 4 MB 之间。", 413)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise QuotaError("请求结构无效。")
                result = {"ok": True}
                if self.path == "/api/import":
                    result["count"] = app.import_documents(data.get("documents"))
                elif self.path == "/api/refresh":
                    app.refresh(data.get("id"))
                elif self.path == "/api/edit":
                    with app.mutation_lock:
                        account = app.vault.get(data.get("id"))
                        label, notes = data.get("label"), data.get("notes")
                        if not isinstance(label, str) or not label.strip() or len(label) > 100 or not isinstance(notes, str) or len(notes) > 1000:
                            raise QuotaError("名称不能为空且最多 100 字，备注最多 1000 字。")
                        account.update(label=label.strip(), notes=notes)
                        app.vault.put(account)
                elif self.path == "/api/remove":
                    with app.mutation_lock:
                        app.vault.remove(data.get("id"))
                elif self.path == "/api/login":
                    result["url"] = app.begin_login()
                elif self.path == "/api/login/cancel":
                    with app.login_lock:
                        if app.login["status"] == "waiting":
                            app.login = {"status": "idle", "message": "登录已取消。"}
                elif self.path == "/api/shutdown":
                    if app.login_server is not None or not app.mutation_lock.acquire(blocking=False):
                        raise QuotaError("有查询或登录正在进行，请完成后再停止。")
                    app.mutation_lock.release()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    raise QuotaError("路径不存在。", 404)
                self.respond(200, result)
            except QuotaError as error:
                self.respond(error.status if error.status in (401, 403, 404, 413, 415) else 400, {"error": str(error)})
            except (ValueError, TypeError):
                self.respond(400, {"error": "请求格式无效。"})
            except Exception:
                self.respond(500, {"error": "本机操作失败，请检查数据目录权限；敏感响应未记录。"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.viewer_token = token
    return server


def main():
    """启动本机服务；--no-browser 供脚本启动和离线验证使用。"""
    parser = argparse.ArgumentParser(description="本机多账户 Codex 配额查看器")
    parser.add_argument("--port", type=int, default=18741)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "CodexQuotaViewer")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    instance_lock = None
    try:
        args.data_dir.mkdir(parents=True, exist_ok=True)
        # 同一数据目录只允许一个服务持有写入锁，避免不同端口同时轮换令牌。
        instance_lock = (args.data_dir / "instance.lock").open("a+b")
        instance_lock.seek(0, 2)
        if instance_lock.tell() == 0:
            instance_lock.write(b"0")
            instance_lock.flush()
        instance_lock.seek(0)
        msvcrt.locking(instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
        app = Application(Vault(args.data_dir / "accounts.dpapi"))
        server = make_server(app, args.port)
    except (QuotaError, OSError):
        print("启动失败：请确认端口未占用、数据目录可读，且使用原 Windows 用户。")
        raise SystemExit(1)
    url = f"http://127.0.0.1:{server.server_port}/#token={server.viewer_token}"
    args.data_dir.mkdir(parents=True, exist_ok=True)
    runtime = args.data_dir / "runtime.dpapi"
    runtime.write_bytes(protect(json.dumps({"port": server.server_port, "token": server.viewer_token}).encode()))
    print("配额查看器已启动，仅监听本机。按 Ctrl+C 停止。", flush=True)
    # 界面会话密钥不打印到日志；仅交给本机浏览器地址片段。
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        instance_lock.close()


if __name__ == "__main__":
    main()
