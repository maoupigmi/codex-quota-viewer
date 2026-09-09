"""使用合成令牌与本机 HTTP 的离线回归测试；不读取实际账户或请求上游。"""

import base64
import copy
import hashlib
import json
import os
import sys
import tempfile
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quota_core import (USAGE_URL, CREDITS_URL, TOKEN_URL, OfficialClient, NoRedirect,
                        QuotaError, normalize_account, parse_quota, parse_credits, instant)
from vault import Vault, protect
from server import Application, make_server


def token(account_id="acct_test", user="user_test", exp=None):
    """创建无效签名的测试令牌，仅供离线字段与账户隔离断言。"""
    payload = {"sub": user, "email": user + "@example.invalid", "exp": exp or time.time()+3600,
               "https://api.openai.com/auth": {"chatgpt_account_id": account_id, "chatgpt_user_id": user, "chatgpt_plan_type": "pro"}}
    return "test." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".unsigned"


def credential(owned=False):
    """返回已标准化的虚构账户，测试刷新令牌始终是哨兵值。"""
    return normalize_account({"access_token": token(), "refresh_token": "REFRESH_SECRET_SENTINEL", "account_id": "acct_test"}, owned=owned)


def usage():
    """用主次窗口逆序验证按真实时长展示，而非猜测五小时与每周。"""
    return {"plan_type": "pro", "rate_limit": {"primary_window": {"used_percent": 3, "limit_window_seconds": 604800, "reset_at": time.time()+600},
                                               "secondary_window": {"used_percent": 100, "limit_window_seconds": 18000, "reset_after_seconds": 0}},
            "additional_rate_limits": [{"limit_name": "专用额度", "rate_limit": {"primary_window": {"used_percent": 12.5, "limit_window_seconds": 3600}}}]}


def plain(raw, decrypt=False):
    """仅测试库使用的可观察存储编码器，生产入口固定使用 DPAPI。"""
    return raw


class FakeClient:
    """记录明确端点并返回测试快照，确保不会接触网络。"""

    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []
        self.refreshes = 0

    def request(self, url, access=None, account_id=None, form=None):
        self.calls.append(url)
        if self.failure == url:
            raise QuotaError("TEST_UPSTREAM_FAILURE", 403)
        if url == USAGE_URL:
            return usage()
        if url == CREDITS_URL:
            return {"available_count": 2, "credits": []}
        if url == TOKEN_URL:
            return {"access_token": token(), "refresh_token": "INDEPENDENT_REFRESH", "expires_in": 3600}
        raise AssertionError("unexpected endpoint")

    def refresh(self, account):
        if not account["owned"]:
            raise QuotaError("副本已过期", 401)
        self.refreshes += 1
        return dict(account, access_token=token(exp=time.time()+7200), refresh_token="ROTATED_SECRET", expires_at=time.time()+7200)


class ParserTests(unittest.TestCase):
    """验证配额口径、未知数据与凭据格式边界。"""

    def test_duration_and_remaining(self):
        windows = parse_quota(usage(), now=1000)["windows"]
        self.assertEqual(windows[0]["remaining"], 97)
        self.assertEqual(windows[0]["window"], "7 天")
        self.assertEqual(windows[1]["remaining"], 0)
        self.assertEqual(windows[1]["reset_at"], 1000)
        self.assertEqual(windows[2]["window"], "1 小时")

    def test_unknown_never_becomes_full(self):
        data = {"rate_limit": {"limit_reached": True, "primary_window": {"reset_at": 10000}}}
        window = parse_quota(data)["windows"][0]
        self.assertIsNone(window["used"])
        self.assertIsNone(window["remaining"])
        self.assertIn("未知", window["window"])

    def test_invalid_percent_values(self):
        for invalid in (True, "NaN", float("inf"), -1, 101, None):
            with self.subTest(invalid=invalid):
                data = {"rate_limit": {"primary_window": {"used_percent": invalid}}}
                self.assertIsNone(parse_quota(data)["windows"][0]["remaining"])

    def test_malformed_usage_rejected(self):
        for data in (None, [], {}, {"rate_limit": []}, {"additional_rate_limits": {}}):
            with self.assertRaises(QuotaError): parse_quota(data)

    def test_camel_case(self):
        data = {"planType":"pro", "rateLimit":{"primaryWindow":{"usedPercent":"0", "limitWindowSeconds":18000, "resetAfterSeconds":10}}}
        result = parse_quota(data, now=1000)
        self.assertEqual(result["windows"][0]["remaining"], 100)
        self.assertEqual(result["windows"][0]["reset_at"], 1010)

    def test_credit_types_status_and_expiry(self):
        cards = [{"reset_type":"codex_rate_limits", "status":"available", "expires_at":2000},
                 {"reset_type":"codex_rate_limits", "status":"used", "expires_at":2000},
                 {"reset_type":"other", "status":"available", "expires_at":2000},
                 {"reset_type":"codex_rate_limits", "status":"available", "expires_at":500}]
        result = parse_credits({"credits":cards}, now=1000)
        self.assertIsNone(result["available"])
        self.assertEqual(len(result["details"]), 3)
        self.assertEqual(sum(c["usable"] for c in result["details"]), 1)

    def test_credit_unknown_not_zero(self):
        with self.assertRaises(QuotaError): parse_credits({"balance":100})
        self.assertEqual(parse_credits({"availableCount":"0"})["available"], 0)
        with self.assertRaises(QuotaError): parse_credits({"available_count":-1})

    def test_import_discards_refresh(self):
        source = {"tokens":{"access_token":token(), "refresh_token":"DO_NOT_REUSE", "account_id":"acct_test"}}
        result = normalize_account(source)
        self.assertFalse(result["owned"])
        self.assertEqual(result["refresh_token"], "")
        self.assertEqual(source["tokens"]["refresh_token"], "DO_NOT_REUSE")

    def test_import_identity_conflicts_and_keys(self):
        for document in ({"OPENAI_API_KEY":"test"}, {"type":"claude", "access_token":token()},
                         {"account_id":"different", "access_token":token()}, {"account_id":"acct_test", "access_token":"a\r\nb"}):
            with self.assertRaises(QuotaError): normalize_account(document)

    def test_users_same_workspace_distinct(self):
        first = normalize_account({"access_token": token(user="one")})
        second = normalize_account({"access_token": token(user="two")})
        self.assertNotEqual(first["id"], second["id"])

    def test_no_ambiguous_timezone(self):
        self.assertIsNone(instant("2026-09-09T10:00:00"))
        self.assertIsNotNone(instant("2026-09-09T10:00:00+08:00"))

    def test_endpoint_whitelist(self):
        for url, form in ((USAGE_URL, {"x":1}), (TOKEN_URL, None), (CREDITS_URL+"/consume", {}), ("https://example.invalid", None)):
            with self.assertRaises(QuotaError): OfficialClient().request(url, form=form)

    def test_no_redirect_credentials(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,"",{},"https://example.invalid"))


class StoreAndServiceTests(unittest.TestCase):
    """验证写入原子性、令牌隔离与错误时保留快照。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "accounts.test"
        self.vault = Vault(self.path, codec=plain)

    def tearDown(self):
        self.directory.cleanup()

    def test_dpapi_roundtrip(self):
        raw = b"SYNTHETIC_SECRET_ONLY"
        cipher = protect(raw)
        self.assertNotIn(raw, cipher)
        self.assertEqual(protect(cipher, decrypt=True), raw)

    def test_public_never_contains_credentials(self):
        self.vault.put(credential(owned=True))
        public = json.dumps(self.vault.public())
        self.assertNotIn("SECRET_SENTINEL", public)
        self.assertNotIn("access_token", public)
        self.assertNotIn("refresh_token", public)

    def test_failed_query_retains_snapshot(self):
        account = credential(); self.vault.put(account)
        app = Application(self.vault, FakeClient())
        app.refresh(account["id"])
        previous = self.vault.get(account["id"])
        app.client.failure = USAGE_URL
        app.refresh(account["id"])
        current = self.vault.get(account["id"])
        self.assertEqual(current["quota"], previous["quota"])
        self.assertEqual(current["quota_at"], previous["quota_at"])
        self.assertIn("FAILURE", current["error"])

    def test_credits_fail_does_not_hide_quota(self):
        account = credential(); self.vault.put(account)
        Application(self.vault, FakeClient(CREDITS_URL)).refresh(account["id"])
        result = self.vault.get(account["id"])
        self.assertIsNotNone(result["quota"])
        self.assertIsNone(result["credits"])
        self.assertTrue(result["credits_error"])

    def test_expired_import_never_refreshes_upstream(self):
        account = credential(); account["expires_at"] = 1; self.vault.put(account)
        client = FakeClient(); Application(self.vault, client).refresh(account["id"])
        self.assertEqual(client.calls, [])
        self.assertEqual(client.refreshes, 0)
        self.assertTrue(self.vault.get(account["id"])["error"])

    def test_rotated_token_persisted_when_usage_fails(self):
        account = credential(owned=True); account["expires_at"] = 1; self.vault.put(account)
        client = FakeClient(USAGE_URL); Application(self.vault, client).refresh(account["id"])
        current = self.vault.get(account["id"])
        self.assertEqual(current["refresh_token"], "ROTATED_SECRET")
        self.assertEqual(client.refreshes, 1)
        self.assertEqual(Vault(self.path, codec=plain).get(account["id"])["refresh_token"], "ROTATED_SECRET")

    def test_import_cannot_replace_owned(self):
        self.vault.put(credential(owned=True))
        with self.assertRaises(QuotaError): self.vault.import_accounts([credential()])
        self.assertTrue(self.vault.get(credential()["id"])["owned"])

    def test_batch_import_atomic(self):
        app = Application(self.vault, FakeClient())
        valid = json.dumps({"access_token":token()})
        with self.assertRaises(QuotaError): app.import_documents([{"content":valid},{"content":"invalid"}])
        self.assertEqual(self.vault.public(), [])

    def test_corrupt_store_not_overwritten(self):
        self.path.write_bytes(b"broken")
        with self.assertRaises(QuotaError): Vault(self.path, codec=plain)
        self.assertEqual(self.path.read_bytes(), b"broken")

    def test_write_failure_keeps_memory(self):
        self.vault.put(credential())
        previous = copy.deepcopy(self.vault.accounts)
        def fail(raw, decrypt=False): raise OSError("test failure")
        self.vault.codec = fail
        item = credential(); item["label"] = "changed"
        with self.assertRaises(OSError): self.vault.put(item)
        self.assertEqual(self.vault.accounts, previous)


class HTTPTests(unittest.TestCase):
    """真实回环 HTTP 测试：认证、跨站防护、固定路由和登录回调。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        vault = Vault(Path(self.directory.name)/"data.test", codec=plain)
        self.app = Application(vault, FakeClient())
        self.server = make_server(self.app, port=0, token="offline-test-session")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.app.login = {"status":"idle", "message":""}
        for _ in range(30):
            if self.app.login_server is None: break
            time.sleep(.05)
        self.directory.cleanup()

    def request(self, path, body=None, headers=None):
        headers = {"X-Viewer-Token":"offline-test-session", "Content-Type":"application/json", **(headers or {})}
        req = urllib.request.Request(self.base+path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with self.opener.open(req, timeout=5) as response: return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_auth_origin_host(self):
        self.assertEqual(self.request('/api/accounts', headers={"X-Viewer-Token":"bad"})[0], 401)
        self.assertEqual(self.request('/api/accounts', headers={"Origin":"https://other.invalid"})[0], 403)
        self.assertEqual(self.request('/api/accounts', headers={"Host":"other.invalid"})[0], 403)

    def test_end_to_end_import_refresh_edit_remove(self):
        document = json.dumps({"access_token":token(),"refresh_token":"DO_NOT_COPY"})
        self.assertEqual(self.request('/api/import', {"documents":[{"content":document}]})[0], 200)
        identity = json.loads(self.request('/api/accounts')[1])["accounts"][0]["id"]
        self.assertEqual(self.request('/api/refresh', {"id":identity})[0], 200)
        self.assertEqual(self.request('/api/edit', {"id":identity,"label":"主力","notes":"备注"})[0], 200)
        public = json.loads(self.request('/api/accounts')[1])["accounts"][0]
        self.assertEqual(public["label"], "主力")
        self.assertEqual(public["quota"]["windows"][0]["remaining"], 97)
        self.assertEqual(self.request('/api/remove', {"id":identity})[0], 200)
        self.assertEqual(json.loads(self.request('/api/accounts')[1])["accounts"], [])

    def test_no_model_or_arbitrary_file_routes(self):
        for path in ('/v1/responses','/api/proxy','/api/consume','/../vault.py','/data/accounts.dpapi'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/api/consume',{})[0], 404)

    def test_pkce_state_and_login_roundtrip(self):
        status, body = self.request('/api/login', {})
        self.assertEqual(status, 200, body)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(json.loads(body)["url"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        callback = 'http://127.0.0.1:1455/auth/callback?'
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.opener.open(callback + 'state=wrong&code=test', timeout=3)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.app.client.calls, [])
        url = callback + urllib.parse.urlencode({"state":query["state"][0],"code":"SYNTHETIC_CODE"})
        with self.opener.open(url, timeout=3) as response: self.assertEqual(response.status, 200)
        self.assertEqual(self.app.login["status"], "success")
        self.assertTrue(self.app.vault.public()[0]["owned"])
        self.assertEqual(self.app.client.calls, [TOKEN_URL])


class LauncherTests(unittest.TestCase):
    """在隔离数据目录验证实际隐藏进程启动、复用与正常停止。"""

    def test_launch_reuse_stop_and_directory_lock(self):
        import launch
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="quota-launch-test-") as directory:
            with patch.dict(os.environ, {"LOCALAPPDATA":directory}), patch("launch.webbrowser.open", return_value=True) as browser:
                child = launch.main()
                runtime = Path(directory)/"CodexQuotaViewer"/"runtime.dpapi"
                info = json.loads(protect(runtime.read_bytes(), decrypt=True))
                try:
                    launch.main()
                    self.assertEqual(browser.call_count, 2)
                    self.assertEqual(json.loads(protect(runtime.read_bytes(), decrypt=True)), info)
                    # 即使另选端口，同一库也必须拒绝第二个进程。
                    attempt = subprocess.run([sys.executable,str(root/'server.py'),'--no-browser','--port','18749'], capture_output=True, timeout=8)
                    self.assertEqual(attempt.returncode, 1)
                    stopped = subprocess.run([sys.executable,str(root/'stop.py')], capture_output=True, timeout=8)
                    self.assertEqual(stopped.returncode, 0)
                finally:
                    request = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/shutdown", data=b'{}',
                        headers={"X-Viewer-Token":info['token'],"Content-Type":"application/json"})
                    try:
                        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2).close()
                    except Exception:
                        pass
                    time.sleep(.8)
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
