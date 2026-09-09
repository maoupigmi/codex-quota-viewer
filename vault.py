"""使用当前 Windows 用户的 DPAPI 保护本机账户数据，不写入原始 auth.json。"""

import copy
import ctypes
import json
import os
import threading
from ctypes import wintypes
from pathlib import Path

from quota_core import QuotaError, jwt_claims


class Blob(ctypes.Structure):
    """与 Windows DATA_BLOB 对齐的字节缓冲区描述。"""
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(raw, decrypt=False):
    """使用当前用户密钥加解密；禁止弹出系统凭据对话框。"""
    if os.name != "nt":
        raise QuotaError("此版本使用 Windows DPAPI，只能在 Windows 运行。")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptProtectData.restype = crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source = Blob(len(raw), buffer)
    output = Blob()
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), "CodexQuotaViewer", None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise QuotaError("账户数据加解密失败；请使用创建数据的 Windows 用户登录。")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


class Vault:
    """串行、原子保存加密账户快照；读失败时拒绝覆盖现有数据。"""

    def __init__(self, path, codec=protect):
        """读取指定数据文件；codec 参数仅用于无真实凭据的离线测试。"""
        self.path = Path(path)
        self.codec = codec
        self.lock = threading.RLock()
        self.accounts = {}
        if self.path.exists():
            try:
                decoded = json.loads(codec(self.path.read_bytes(), decrypt=True))
                if decoded.get("version") != 1 or not isinstance(decoded.get("accounts"), dict):
                    raise ValueError()
                self.accounts = decoded["accounts"]
            except QuotaError:
                raise
            except (OSError, ValueError, AttributeError):
                raise QuotaError("账户库损坏或版本不支持；原文件未修改。") from None

    def commit(self, accounts):
        """先完成加密与原子替换，再更新内存，避免写入失败造成状态不一致。"""
        raw = json.dumps({"version": 1, "accounts": accounts}, ensure_ascii=False, allow_nan=False).encode()
        encrypted = self.codec(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        self.accounts = copy.deepcopy(accounts)

    def get(self, identity):
        """返回隔离副本，调用者不能绕过原子保存修改内存凭据。"""
        with self.lock:
            if identity not in self.accounts:
                raise QuotaError("账户不存在或已移除。", 404)
            return copy.deepcopy(self.accounts[identity])

    def put(self, account):
        """写入一个已验证账户；调用方负责同账户刷新串行化。"""
        with self.lock:
            accounts = copy.deepcopy(self.accounts)
            accounts[account["id"]] = account
            self.commit(accounts)

    def import_accounts(self, incoming):
        """整批验证后一次写入；不允许导入副本覆盖独立登录的刷新凭据。"""
        with self.lock:
            accounts = copy.deepcopy(self.accounts)
            for item in incoming:
                previous = accounts.get(item["id"])
                if previous and previous["owned"] and not item["owned"]:
                    raise QuotaError("同一账户已独立登录，不能用导入副本覆盖；请先移除本地记录。")
                if previous:
                    for key in ("notes", "label", "created_at"):
                        item[key] = previous[key]
                accounts[item["id"]] = item
            self.commit(accounts)

    def remove(self, identity):
        """只删除本工具保存的本地记录，不触碰上游账户与原凭据文件。"""
        with self.lock:
            accounts = copy.deepcopy(self.accounts)
            if identity not in accounts:
                raise QuotaError("账户不存在。", 404)
            del accounts[identity]
            self.commit(accounts)

    def public(self):
        """按白名单返回展示字段，访问令牌和刷新令牌从不进入前端响应。"""
        keys = ("id", "name", "label", "email", "account_id", "owned", "expires_at", "plan_hint", "subscription_until", "quota", "quota_at", "credits", "credits_at", "last_attempt", "error", "credits_error", "notes")
        with self.lock:
            result = []
            for account in self.accounts.values():
                item = {key: copy.deepcopy(account.get(key)) for key in keys}
                # 已导入的旧记录从其本机访问令牌读取名称，无需重新导入或改写原凭据。
                if not item.get("name"):
                    profile = jwt_claims(account.get("access_token")).get("https://api.openai.com/profile", {})
                    name = profile.get("name") if isinstance(profile, dict) else None
                    item["name"] = name.strip()[:100] if isinstance(name, str) else ""
                result.append(item)
            return result
