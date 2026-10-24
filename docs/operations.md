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

编辑 `.env`，至少设置：

- `LLM_API_KEY`：OpenAI 或 OpenAI-compatible 服务的密钥。
- `LLM_MODEL`：实际模型名称。
- `LLM_BASE_URL`：兼容供应商端点；官方 OpenAI 留空。
- `LLM_API_STYLE`：`responses` 或 `chat_completions`，不自动探测。
- `CONTEXT_WINDOW_TOKENS`：所用模型的真实上下文窗口，不能保留为 `0`。

推理相关配置：

- `LLM_REASONING_CAPTURE=auto|structured|tagged|both`：选择结构化字段或 `<think>` 来源。
- `LLM_REASONING_OUTPUT=auto|raw|summary|off`：选择向 SSE 输出的推理内容类型。
- `LLM_REASONING_EFFORT`、`LLM_REASONING_SUMMARY`：仅在所选 API/模型支持时设置。
- `LLM_THINK_TAG_ENABLED` 和 `LLM_THINK_TAG_NAME`：控制正文起始标签解析。
- `LLM_MAX_OUTPUT_TOKENS`：普通 Agent 回合的输出上限。

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
- 恢复时先停止服务，用备份替换数据库，再启动服务。启动恢复会把遗留的 running Run
  标记为 `PROCESS_RESTARTED`，已提交的 SSE 事件仍可重放。
- 原始消息不会因 Context 摘要而删除；容量规划应同时考虑 messages、traces 和
  stream_events。

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
- `PROCESS_RESTARTED`：上一次进程在 Run 完成前退出，需要重新提交用户请求。
- `CONTEXT_LIMIT_EXCEEDED`：最近 5 轮和当前输入本身已接近窗口；换用更大窗口模型或缩短
  输入。
- 天气错误：检查外网、Open-Meteo 状态和超时配置；错误会作为结构化工具结果返回 Agent。

诊断时通过 `GET /v1/runs/{run_id}` 和 `GET /v1/runs/{run_id}/trace` 关联 Run。Trace 和
错误响应不包含 API Key。模型实际返回的可见推理只进入 `reasoning.*` 流事件，不写入
assistant 最终消息。

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
并发同键、响应丢失后重试、参数冲突、事务回滚和结果未知超时测试。当前三个工具均为
纯计算或只读，因此本版本不提前创建 `tool_operations` 表，也不对非幂等写操作盲重试。
