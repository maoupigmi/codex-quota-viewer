# Codex Quota Viewer · 配额查看器

从 [CLIProxyAPI（CPA）](https://github.com/router-for-me/CLIProxyAPI) 及其 [管理面板](https://github.com/router-for-me/Cli-Proxy-API-Management-Center) 的 Codex 配额查询功能拆分、提取并用 Python 重写的 Windows 本机工具。无需运行 CPA 网关、sub2api、数据库或 npm。

**本项目是独立维护的衍生工具，不是 OpenAI 或 Router-For.ME 的官方产品，也未获其背书。** 上游项目及其作者的贡献、版权与 MIT 许可证均予保留，详见 [来源与改动说明](THIRD_PARTY_NOTICES.md)、[项目许可证](LICENSE)。Codex、ChatGPT 等名称仅用于说明兼容对象，其权利归各自所有者。

本项目只查询授权账户的额度与重置卡信息，不提供模型请求转发或重置卡消耗操作。MIT 许可覆盖本项目的代码使用，不授予第三方服务的访问权，也不替代第三方服务的条款。

## 使用

1. 双击 `Start.cmd`，会在默认浏览器打开看板，服务在后台运行。重复打开会复用已有实例。
2. 点击「登录账户」，在官方登录页面完成登录，然后回到看板。可以重复添加多个账户。
3. 点击账户「刷新」或「刷新全部」，查看上游返回的额度与重置卡。
4. 不需要时双击 `Stop.cmd`，正常停止本工具，不影响其他 Python 或 Codex 进程。

运行环境：Windows、Python 3.10+，并确保 `python` 命令可用。开发验证使用 Python 3.13；本版本未打包为 EXE。

也可以点击「导入 JSON」，多选以下文件：

- Codex CLI 的 `auth.json`：`tokens.access_token`、`tokens.account_id` 等字段。
- CPA 的 Codex 凭据文件：平铺的 `access_token`、`account_id` 等字段，`type` 必须是 `codex` 或未提供。

不支持只有 API Key 或只有 refresh_token 的 JSON。缺少 account_id、账户身份冲突、文件格式错误时，整批导入拒绝写入，不猜测账户。

## 独立登录与导入副本

| 模式 | 凭据生命周期 |
|---|---|
| 独立登录 | 本工具取得自己的登录令牌；手动查询时若令牌过期，刷新并保存轮换后的令牌 |
| 导入副本 | 只保存访问令牌，不保存和使用导入的 refresh_token；过期后需要重新导入 |

这样避免多个客户端使用同一 refresh_token 并发轮换。工具不扫描本机账户目录，不修改源 `auth.json`，不切换官方 Codex 客户端账户。

## 显示内容

账户卡片默认突出四项：通用剩余额度、周额度恢复、可用重置卡、重置卡最近到期。其他额度、全部卡片明细、凭据到期、备注及编辑/移除操作收在「更多信息」中。查询失败提示保持可见。

- 账户名称、邮箱（凭据提供时）、上游套餐名称与备注。
- 通用、代码审查和上游返回的附加额度窗口。
- **剩余百分比 = 100 − 上游 used_percent**，不会用请求次数估算订阅额度。
- 窗口时长按 `limit_window_seconds` 显示，不默认把主窗口当 5 小时、次窗口当 7 天。
- 恢复时间按当前浏览器本地时区显示；到达时间后标为待刷新，不自动把额度改为 100%。
- 重置卡可用数量、当前适用数量和可用卡的到期时间（接口提供时）。不调用消耗重置卡的接口。
- 搜索、账户名称排序、通用剩余额度排序、最近恢复排序、重置卡即将到期排序。

「近期已更新」指 5 分钟内查询成功、未出现错误且窗口尚未经过恢复时间的账户。其余快照不用于剩余额度优先排序。上游配额本身不是实时推送。

未知字段显示未知；查询失败保留上次成功快照并显示错误。重置卡查询失败不影响已查询成功的额度。重置卡数量不会根据明细数量推测，也不把 credits 积分余额当成重置卡。

## 本机数据与网络

数据目录：`%LOCALAPPDATA%\CodexQuotaViewer`

- `accounts.dpapi`：当前 Windows 用户 DPAPI 加密的账户及快照。
- `runtime.dpapi`：加密的本地页面会话信息，用于重复打开和停止实例。
- `instance.lock`：同一账户库的进程写入锁。

服务只监听 `127.0.0.1:18741`。独立登录期间临时监听 `127.0.0.1:1455`，使用 PKCE 和随机 state，5 分钟后自动结束。会话密钥经地址片段交给页面后立即从地址栏移除；存放在当前页签 sessionStorage，账户令牌不返回前端。关闭所有相关页签后，重新运行启动脚本打开。

上游网络端点仅有：

| 方法 | 地址 | 用途 |
|---|---|---|
| GET | `https://chatgpt.com/backend-api/wham/usage` | 配额窗口 |
| GET | `https://chatgpt.com/backend-api/wham/rate-limit-reset-credits` | 重置卡明细 |
| POST | `https://auth.openai.com/oauth/token` | 独立登录兑换/刷新令牌 |

浏览器登录跳转至 `https://auth.openai.com/oauth/authorize`。程序没有模型请求、任意地址转发、API Key 分发或额度重置入口。

Python 网络请求沿用环境或系统代理设置，保留 TLS 证书验证，不跟随携带凭据的重定向。不绕过网络验证页。`HTTPS_PROXY` 可指定 HTTP 代理；本版本未引入 SOCKS 库。

`/backend-api/wham/*` 是非公开接口，其可用性和字段可能变化。403、限流或未知结构都会显示错误，不构造替代结果。

## 验证记录

2026-09-09，Windows / Python 3.13 验证记录：

- 28 项离线测试通过，包括 DPAPI 实际加解密、账户隔离、整批导入原子性、保留旧快照、凭据轮换保存、HTTP 认证与跨站校验。
- PKCE 回调流程以合成授权码和测试客户端完成；没有向官方兑换真实令牌。
- 隔离数据目录下实际启动隐藏后台进程，重复启动复用、同库进程锁和正常停止通过。
- Chrome 界面验证了搜索、名称/备注编辑、手动刷新、排序控件及页面重新加载；合成文字中的 HTML 标签按纯文本显示，浏览器没有记录 JS 错误。
- 已在获授权的真实账户上完成凭据副本导入、配额与重置卡查询及 Chrome 展示；源凭据文件哈希未变化，未消耗重置卡。独立 OAuth 真实登录与令牌轮换尚未验收。

运行离线测试（先停止本工具；启动器测试需要本机 18741 端口，登录回调测试需要 1455 端口）：

```powershell
# 在项目根目录运行
python -m unittest discover -s tests -v
```

查看启动错误可运行 `python server.py`，默认会尝试打开浏览器，Ctrl+C 停止。自定义数据目录或端口只用于手动运行，默认启动/停止脚本使用默认目录与端口。

## 文件说明与来源

| 文件 | 职责 |
|---|---|
| `quota_core.py` | 凭据格式、HTTP 白名单、额度/重置卡解析与令牌刷新 |
| `vault.py` | DPAPI 及账户库原子写入 |
| `server.py` | 本机 API、独立登录回调与服务生命周期 |
| `web/` | 中文看板界面 |
| `launch.py` / `stop.py` | 启动、复用与停止 |
| `tests/` | 合成数据回归测试与临时界面验收服务 |
| `reference/` | 上游 MIT 许可证及读取快照的 SHA-256 清单；不参与运行 |

源代码按 MIT 许可分发；请一并保留 [上游署名和许可证](THIRD_PARTY_NOTICES.md)。隐私与问题报告说明见 [SECURITY.md](SECURITY.md)。
