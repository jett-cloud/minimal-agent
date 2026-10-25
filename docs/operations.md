# Minimal Agent 部署与维护

## 运行边界

- Python 3.12。
- 单进程、单 Uvicorn worker。RunSupervisor 和 SSE 通知器是进程内组件，多 worker 会让
  后台任务和通知状态分散。
- 服务默认没有身份认证。开发环境只监听 `127.0.0.1`；部署到共享网络前应在反向代理
  增加 TLS、认证、请求大小限制和访问日志脱敏。
- SQLite 适合这个单进程 MVP。需要多实例或水平扩展时，应同时替换任务队列、通知机制
  和数据库，而不是只增加 Uvicorn worker。

## 从源码启动

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

`.env.example` 默认只留下两个待填写项：

- `LLM_API_KEY`：OpenAI 或 OpenAI-compatible 服务的密钥。
- `LLM_MODEL`：实际模型名称。

使用 OpenAI-compatible 供应商时，再取消注释并设置 `LLM_BASE_URL`；官方 OpenAI 不需要。
`LLM_API_STYLE` 默认是 `responses`，仅在供应商或模型只支持 Chat Completions 时改为
`chat_completions`。其余项目全部是可选覆盖项，保持注释会使用 `Settings` 中的默认值。

启动期间会请求 OpenAI-compatible `/models` 接口，按 `LLM_MODEL` 精确匹配并
读取 `context_window`（同时兼容 `context_length` 和 `max_context_tokens`）。该值在
进程内缓存，不再由 `.env` 手工配置。模型目录不可用、找不到当前模型或
窗口元数据非法时，健康检查和 Session 查询仍可用，消息提交返回
`503 AGENT_UNAVAILABLE`。

推理相关配置：

- `LLM_REASONING_CAPTURE=auto|structured|tagged|both`：选择结构化字段或 `<think>` 来源。
- `LLM_REASONING_OUTPUT=auto|raw|summary|off`：选择向 SSE 输出的推理内容类型。
- `LLM_REASONING_EFFORT`、`LLM_REASONING_SUMMARY`：仅在所选 API/模型支持时设置。
- `LLM_THINK_TAG_ENABLED` 和 `LLM_THINK_TAG_NAME`：控制正文起始标签解析。

普通 Agent 请求不发送 `max_output_tokens`/`max_tokens`，由模型服务决定单次输出能力；
Run 和内置工具也没有统一的应用层总时限。`LLM_TIMEOUT_SECONDS` 仅配置 LLM HTTP 客户端的
连接、读取、写入和连接池等待超时，不是完整流生成的墙钟截止时间；天气上游对应
`WEATHER_HTTP_TIMEOUT_SECONDS`。订单工具单独使用 `ORDER_TOOL_TIMEOUT_SECONDS`；超时后
执行器按工具声明最多重试一次。终止无限循环仍由 `AGENT_MAX_STEPS` 的默认 8 步上限保证。

启动：

```powershell
.venv\Scripts\python -m uvicorn minimal_agent.main:app --host 127.0.0.1 --port 8000 --workers 1
```

未配置 LLM 时 `/health` 和 Session 查询仍然可用，消息提交会返回
`503 AGENT_UNAVAILABLE`，不会在启动时泄漏密钥或崩溃。

## 最小调用流程

```powershell
$session = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/sessions `
  -ContentType application/json -Body '{"title":"demo"}'

$accepted = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/v1/sessions/$($session.id)/messages" `
  -ContentType application/json -Body '{"text":"请计算 12 * 8"}'

curl.exe -N "http://127.0.0.1:8000$($accepted.events_url)"
```

断线后把最后收到的 SSE `id` 放入 `Last-Event-ID` 请求头，或传入 `after_id` 查询参数。

## wheel 构建与安装

```powershell
.venv\Scripts\python -m build
python -m venv .wheel-venv
.wheel-venv\Scripts\python -m pip install dist\minimal_agent-0.1.0-py3-none-any.whl
```

wheel 包含 SQLite schema；提示词和主板数据安装到环境的 `share/minimal-agent`。配置未显式
覆盖资源路径时，程序会先使用源码目录资源，再使用已安装资源。

## 数据和备份

- 默认数据库：`var/minimal_agent.db`。
- SQLite 使用 WAL。备份前优先优雅停止服务，然后复制数据库文件；运行中备份应使用
  SQLite 官方 `.backup` 能力，不应只复制主数据库文件而忽略 WAL。
- 恢复时先停止服务，用备份替换数据库，再启动服务。启动恢复会从完整的持久化工具调用
  边界继续；中断在模型流或缺少稳定边界的 running Run 标记为 `PROCESS_RESTARTED`。
- 原始消息不会因 Context 摘要而删除；容量规划应同时考虑 messages、traces、
  stream_events、tool_invocations，以及订单模块的 orders 和 order_operations。
- `DELETE /v1/sessions/{session_id}` 会级联删除该 Session 的消息、Run、Trace 和流事件，
  操作不可恢复；有 running Run 时返回 `409 SESSION_BUSY`。

## 主板数据维护

数据字段、官方来源和核验规则见 [`../data/README.md`](../data/README.md)。更新后至少执行：

```powershell
.venv\Scripts\python -m pytest tests\unit\tools\test_search.py
```

数据集会在工具初始化时由 Pydantic 完整校验；重复 ID、缺失字段或非法来源会让 Agent
运行时进入不可用状态，而健康检查和 Session 查询仍保持可用。

## 故障排查

- `AGENT_UNAVAILABLE`：检查 Key、模型、base URL、Context 窗口和资源路径。
- `SESSION_BUSY`：同一 Session 已有 running Run；订阅原 Run 的 SSE 或等待其终止。
- `PROCESS_RESTARTED`：上一次进程在模型流或其他不可恢复边界退出，需要重新提交请求。
- `TOOL_RESULT_UNKNOWN`：工具的允许尝试均超时且结果仍不确定；先查询业务状态或逻辑调用
  记录，避免手工重复提交写操作。
- `CONTEXT_LIMIT_EXCEEDED`：最近 5 轮和当前输入本身已接近窗口；换用更大窗口模型或缩短
  输入。
- 天气错误：检查外网、Open-Meteo 状态和超时配置；错误会作为结构化工具结果返回 Agent。

诊断时通过 `GET /v1/runs/{run_id}` 和 `GET /v1/runs/{run_id}/trace` 关联 Run；通过
`GET /v1/runs/{run_id}/event-history` 分页恢复事件，通过 `/reasoning` 查看从持久化
`reasoning.*` 事件重建的历史推理。Vue 重新进入 Session 时自动恢复最近 Run 的事件与
Trace，并按回合把推理放在对应工具或回答之前。Trace 和错误响应不包含 API Key；推理
不重复写入 assistant 最终消息。

## 验证

```powershell
.venv\Scripts\python -m pytest -ra
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m build
```

真实模型测试默认跳过。只有显式设置 `RUN_LIVE_LLM_TESTS=1` 时才会调用付费 API。
`LIVE_TEST_PROFILE=smoke` 默认最多 1 次调用；`full` 默认最多 8 次，覆盖三个工具、压缩
和推理。完整环境变量和命令见 README。

## 新工具测试与写操作约束

每个注册工具必须在 `tests/contract/test_tool_contracts.py` 登记，并分别拥有单元测试和
Agent 集成测试。`ToolDefinition` 必须声明 `side_effect` 和 `retry_semantics`。

首次增加 create/update/delete 等写工具时，需要同时增加持久化的 operation ID/幂等键、
并发同键、响应丢失后重试、参数冲突、事务回滚和结果未知超时测试。订单写工具已使用
`order_operations` 持久化成功响应；相同键同请求重放，相同键不同请求拒绝。更新和删除
额外使用版本号阻止并发覆盖。框架将每次模型工具决策写入 `tool_invocations`，并通过
`ToolExecutionContext` 将幂等键传给写工具，模型不负责生成技术键。
