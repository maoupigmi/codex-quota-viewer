"""独立的 Codex 配额协议层：仅查询额度、重置卡和管理本工具的登录令牌。"""

import base64
import hashlib
import json
import math
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
TOKEN_URL = "https://auth.openai.com/oauth/token"
AUTH_URL = "https://auth.openai.com/oauth/authorize"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
MAX_RESPONSE = 2 * 1024 * 1024


class QuotaError(Exception):
    """可直接显示的错误；不包含令牌、响应正文或带敏感参数的地址。"""

    def __init__(self, message, status=None):
        """记录安全错误信息及上游 HTTP 状态，供登录失效处理使用。"""
        super().__init__(message)
        self.status = status


def field(obj, snake, camel=None):
    """读取源码明确支持的两种字段命名，不猜测其他响应结构。"""
    if not isinstance(obj, dict):
        return None
    value = obj.get(snake)
    return obj.get(camel) if value is None and camel else value


def number(value):
    """解析有限数字；缺失、布尔值和 NaN 均不能当作零额度。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def instant(value):
    """解析 Unix 秒或带时区的 ISO 时间；不猜测无时区字符串。"""
    numeric = number(value)
    if numeric is not None:
        return numeric if 0 < numeric < 253402300799 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


def jwt_claims(token):
    """仅读取 JWT 元数据用于账户标签与到期提示；不作为签名验证结果。"""
    if not isinstance(token, str) or len(token) > 100000:
        return {}
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return data if isinstance(data, dict) else {}
    except (ValueError, IndexError, UnicodeError):
        return {}


def normalize_account(document, label="", owned=False):
    """接收 Codex auth.json 或 CPA 平铺 JSON；导入副本不保留刷新令牌。"""
    if not isinstance(document, dict):
        raise QuotaError("凭据必须是一个 JSON 对象。")
    if document.get("type") not in (None, "codex"):
        raise QuotaError("这里只支持 Codex 账户凭据。")
    tokens = document.get("tokens", document)
    if not isinstance(tokens, dict):
        raise QuotaError("tokens 字段必须是对象。")
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access or len(access) > 100000 or not access.isascii() or any(c.isspace() for c in access):
        raise QuotaError("缺少有效 access_token；不支持 API Key 文件或只含 refresh_token 的文件。")
    claims = jwt_claims(tokens.get("id_token"))
    access_claims = jwt_claims(access)
    auth = claims.get("https://api.openai.com/auth", {})
    access_auth = access_claims.get("https://api.openai.com/auth", {})
    if not isinstance(auth, dict) or not isinstance(access_auth, dict):
        raise QuotaError("凭据中的账户元数据结构无效。")
    account_id = tokens.get("account_id") or document.get("account_id") or auth.get("chatgpt_account_id") or access_auth.get("chatgpt_account_id")
    if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", account_id):
        raise QuotaError("缺少明确的 ChatGPT account_id，无法安全区分账户。")
    for known in (auth.get("chatgpt_account_id"), access_auth.get("chatgpt_account_id")):
        if known and known != account_id:
            raise QuotaError("account_id 与令牌元数据不一致，已拒绝导入。")
    profile = access_claims.get("https://api.openai.com/profile", {})
    email = claims.get("email") or (profile.get("email") if isinstance(profile, dict) else None) or document.get("email") or ""
    # 显示名称来自登录令牌元数据，与用户手工设置的账户备注名称分开保存。
    name = claims.get("name") or (profile.get("name") if isinstance(profile, dict) else None)
    name = name.strip()[:100] if isinstance(name, str) else ""
    user_id = auth.get("chatgpt_user_id") or access_auth.get("chatgpt_user_id") or claims.get("sub") or access_claims.get("sub") or email
    identity = hashlib.sha256(f"{account_id}\0{user_id}".encode()).hexdigest()[:24]
    refresh = tokens.get("refresh_token", "") if owned else ""
    if not isinstance(refresh, str) or len(refresh) > 100000:
        raise QuotaError("刷新令牌格式无效。")
    expires_at = instant(access_claims.get("exp")) or instant(document.get("expired"))
    if number(document.get("expires_in")) is not None:
        expires_at = time.time() + float(document["expires_in"])
    return {
        "id": identity, "account_id": account_id, "email": str(email)[:200], "name": name,
        "label": str(label or email or account_id)[:100], "owned": owned,
        "access_token": access, "refresh_token": refresh, "expires_at": expires_at,
        "plan_hint": str(auth.get("chatgpt_plan_type") or access_auth.get("chatgpt_plan_type") or "")[:80],
        "subscription_until": instant(auth.get("chatgpt_subscription_active_until")),
        "created_at": time.time(), "quota": None, "quota_at": None,
        "credits": None, "credits_at": None, "last_attempt": None,
        "error": "", "credits_error": "", "notes": "",
    }


def parse_quota(payload, now=None):
    """解析已使用百分比及实际窗口时长；缺值显示未知，不推定已满或每周。"""
    now = time.time() if now is None else now
    if not isinstance(payload, dict):
        raise QuotaError("额度接口返回的内容不是对象。")
    groups = [("通用额度", field(payload, "rate_limit", "rateLimit")),
              ("代码审查", field(payload, "code_review_rate_limit", "codeReviewRateLimit"))]
    additional = field(payload, "additional_rate_limits", "additionalRateLimits")
    if additional is not None and not isinstance(additional, list):
        raise QuotaError("附加额度结构已变化，请更新解析器。")
    for item in additional or []:
        if not isinstance(item, dict):
            raise QuotaError("附加额度项结构无效。")
        name = field(item, "limit_name", "limitName") or field(item, "metered_feature", "meteredFeature") or "未命名附加额度"
        groups.append((str(name)[:160], field(item, "rate_limit", "rateLimit")))
    windows = []
    warnings = []
    for name, group in groups:
        if group is None:
            continue
        if not isinstance(group, dict):
            raise QuotaError("额度分组结构无效。")
        for key, camel, fallback_label in (("primary_window", "primaryWindow", "主窗口"), ("secondary_window", "secondaryWindow", "次窗口")):
            window = field(group, key, camel)
            if window is None:
                continue
            if not isinstance(window, dict):
                raise QuotaError("额度窗口结构无效。")
            used = number(field(window, "used_percent", "usedPercent"))
            if used is None or not 0 <= used <= 100:
                used = None
                warnings.append(f"{name} / {fallback_label}：使用比例缺失或无效")
            seconds = number(field(window, "limit_window_seconds", "limitWindowSeconds"))
            duration = f"{seconds / 3600:g} 小时" if seconds and seconds > 0 else fallback_label + "（周期未知）"
            if seconds and seconds >= 86400 and seconds % 86400 == 0:
                duration = f"{seconds / 86400:g} 天"
            reset = instant(field(window, "reset_at", "resetAt"))
            offset = number(field(window, "reset_after_seconds", "resetAfterSeconds"))
            if reset is None and offset is not None and offset >= 0:
                reset = now + offset
            windows.append({"group": name, "window": duration, "seconds": seconds,
                            "used": used, "remaining": None if used is None else round(100 - used, 4),
                            "reset_at": reset, "allowed": group.get("allowed"),
                            "limit_reached": field(group, "limit_reached", "limitReached")})
    if not windows:
        raise QuotaError("接口未返回可识别的额度窗口；旧快照已保留。")
    return {"plan": str(field(payload, "plan_type", "planType") or "未知")[:80],
            "windows": windows, "warnings": warnings}


def parse_credits(payload, now=None):
    """解析重置卡明细；数量只采用上游明确返回值，不将积分余额视作重置卡。"""
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or not any(k in payload for k in ("credits", "available_count", "availableCount", "applicable_available_count", "applicableAvailableCount")):
        raise QuotaError("重置卡接口结构无法识别。")
    available = number(field(payload, "available_count", "availableCount"))
    applicable = number(field(payload, "applicable_available_count", "applicableAvailableCount"))
    for count in (available, applicable):
        if count is not None and (count < 0 or count != int(count)):
            raise QuotaError("重置卡数量无效。")
    raw = payload.get("credits")
    if raw is not None and not isinstance(raw, list):
        raise QuotaError("重置卡明细结构无效。")
    details = []
    for item in raw or []:
        if not isinstance(item, dict) or field(item, "reset_type", "resetType") != "codex_rate_limits":
            continue
        expiry = instant(field(item, "expires_at", "expiresAt"))
        status = str(item.get("status", "未知"))[:60]
        details.append({"id": str(item.get("id", ""))[:120], "status": status,
                        "granted_at": instant(field(item, "granted_at", "grantedAt")), "expires_at": expiry,
                        "usable": status == "available" and expiry is not None and expiry > now})
    return {"available": available, "applicable": applicable, "details": details,
            "details_present": isinstance(raw, list)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止上游重定向，确保授权头只发送给明确指定的官方端点。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """不跟随重定向；由请求层返回经过脱敏的 HTTP 错误。"""
        return None


class OfficialClient:
    """固定上游地址的 HTTP 客户端，没有任意 URL 转发或模型调用入口。"""

    def __init__(self):
        """沿用系统或环境代理设置，保留 TLS 验证。"""
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, url, access=None, account_id=None, form=None):
        """最多读取 2 MB，禁止输出上游正文；查询与 OAuth 端点严格白名单。"""
        if (url == TOKEN_URL) != (form is not None) or url not in (USAGE_URL, CREDITS_URL, TOKEN_URL):
            raise QuotaError("请求不在只读配额与登录端点白名单中。")
        headers = {"Accept": "application/json", "User-Agent": "CodexQuotaViewer/0.1 (Windows)"}
        if access:
            headers["Authorization"] = "Bearer " + access
            headers["Chatgpt-Account-Id"] = account_id
        if url == CREDITS_URL:
            headers.update({"OpenAI-Beta": "codex-1", "Originator": "Codex Desktop"})
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self.opener.open(req, timeout=25) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise QuotaError("上游响应过大，已停止读取。")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise QuotaError("上游 JSON 结构无效。")
            return result
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            hints = {401: "登录已失效，请重新登录或重新导入凭据。", 403: "访问被拒绝，请检查账户权限或网络。", 429: "查询受到限流，请稍后手动刷新。"}
            raise QuotaError(hints.get(status, "上游请求失败。") + f"（HTTP {status}）", status) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise QuotaError("网络连接失败或超时；请检查系统代理和网络。") from None
        except (ValueError, UnicodeError):
            raise QuotaError("上游未返回有效 JSON；可能是网络验证页或接口变化。") from None

    def refresh(self, account):
        """只刷新本工具独立登录产生的凭据，防止轮换其他客户端的刷新令牌。"""
        if not account.get("owned") or not account.get("refresh_token"):
            raise QuotaError("导入凭据已过期，请重新导入；副本模式不会刷新原客户端的令牌。", 401)
        result = self.request(TOKEN_URL, form={"client_id": CLIENT_ID, "grant_type": "refresh_token",
                                              "refresh_token": account["refresh_token"], "scope": "openid profile email"})
        access = result.get("access_token")
        if not isinstance(access, str) or not access:
            raise QuotaError("令牌刷新响应缺少 access_token，请重新登录。")
        auth = jwt_claims(access).get("https://api.openai.com/auth", {})
        if isinstance(auth, dict) and auth.get("chatgpt_account_id") not in (None, account["account_id"]):
            raise QuotaError("刷新后的账户身份不一致，已停止查询。")
        updated = dict(account, access_token=access)
        refresh = result.get("refresh_token")
        if isinstance(refresh, str) and refresh:
            updated["refresh_token"] = refresh
        duration = number(result.get("expires_in"))
        updated["expires_at"] = time.time() + duration if duration and duration > 0 else instant(jwt_claims(access).get("exp"))
        return updated
