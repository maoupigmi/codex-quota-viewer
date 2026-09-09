"""临时界面验收服务：全部为合成账户，不连接官方服务，不写生产账户库。"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quota_core import normalize_account, parse_quota, parse_credits
from server import Application, make_server
from vault import Vault
from test_viewer import FakeClient, token, plain


def main():
    """展示充足、不足和失败三种状态，关闭服务即删除临时合成数据。"""
    with tempfile.TemporaryDirectory(prefix="quota-viewer-ui-") as directory:
        vault = Vault(Path(directory)/"fixture.json", codec=plain)
        now = time.time()
        for index, (name, used) in enumerate((("演示 · 主力账户", 17), ("演示 · 备用账户", 94), ("演示 · 待重新登录", 45))):
            account = normalize_account({"access_token":token(user=f"demo{index}"), "refresh_token":"fake"}, label=name, owned=index==0)
            account["quota"] = parse_quota({"plan_type":"pro", "rate_limit":{
                "primary_window":{"used_percent":used,"limit_window_seconds":18000,"reset_at":now+3900},
                "secondary_window":{"used_percent":used/2,"limit_window_seconds":604800,"reset_at":now+180000}}})
            account["quota_at"] = now if index != 2 else now-7200
            account["credits"] = parse_credits({"available_count":2,"applicable_available_count":2,"credits":[
                {"id":"fake-1","reset_type":"codex_rate_limits","status":"available","expires_at":now+16000},
                {"id":"fake-2","reset_type":"codex_rate_limits","status":"available","expires_at":now+864000}]})
            account["credits_at"] = now
            account["notes"] = "合成数据，仅用于界面验收；没有接入实际账户。"
            if index == 2:
                account["error"] = "登录已失效，请重新导入凭据。（HTTP 401）"
                account["credits_error"] = "本次查询失败，显示上次快照。"
            vault.put(account)
        server = make_server(Application(vault, FakeClient()), 18742, "synthetic-ui-session")
        print("OFFLINE_UI http://127.0.0.1:18742/#token=synthetic-ui-session", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
