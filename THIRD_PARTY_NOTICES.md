# 上游来源与许可证

2026-09-09 获取以下项目 main 分支，参考其 Codex 配额协议和 OAuth 协议后用 Python 实现独立只读工具。不是官方 OpenAI 客户端，也不是 CPA 网关的完整分发。

## CLIProxyAPI

- 项目：https://github.com/router-for-me/CLIProxyAPI
- OAuth 来源：[internal/auth/codex/openai_auth.go](https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/auth/codex/openai_auth.go)
- 配额协议补充：[internal/runtime/executor/helps/codex_quota.go](https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/runtime/executor/helps/codex_quota.go)
- MIT 许可证原文：`reference/CLIProxyAPI__LICENSE`

保留 OAuth client_id、PKCE 参数及 token endpoint 协议；未复制网关调度、模型调用或重试系统。导入凭据不刷新，只有本工具独立登录的凭据可刷新。

## Cli-Proxy-API-Management-Center

- 项目：https://github.com/router-for-me/Cli-Proxy-API-Management-Center
- 查询地址：[src/utils/quota/constants.ts](https://github.com/router-for-me/Cli-Proxy-API-Management-Center/blob/main/src/utils/quota/constants.ts)
- 查询组合：[src/features/quota/providers/codex/data.ts](https://github.com/router-for-me/Cli-Proxy-API-Management-Center/blob/main/src/features/quota/providers/codex/data.ts)
- 重置卡字段：[src/utils/quota/resetCredits.ts](https://github.com/router-for-me/Cli-Proxy-API-Management-Center/blob/main/src/utils/quota/resetCredits.ts)
- MIT 许可证原文：`reference/Cli-Proxy-API-Management-Center__LICENSE`

从 `api-call` 中提取 GET 查询，在独立后端固定地址调用。重新实现轻量中文界面与严格解析；不采用缺少比例时推定 100%、按主次顺序猜测窗口周期、按明细推定可用卡数量等逻辑。没有提取消耗重置卡的操作。

上游 main 随时会变化。提取时读取的上游参考文件 SHA-256 见 `reference/SHA256SUMS.txt`；这些哈希标识本次读取的文件，不冒充上游 Git 提交 ID。公开仓库不打包这些参考源码全文；可通过上述上游链接查阅原文。

分发本工具时请保留此说明及两个上游 MIT 许可证。
