# Minimal Agent

一个不依赖 Agent 框架的最小可用 ReAct Agent，使用 FastAPI、Pydantic、SQLite、
OpenAI Python SDK、pytest 和 Ruff。

项目采用逐步 Review 的开发方式。当前状态、Review 结论和下一步计划以
[`docs/progress.md`](docs/progress.md) 为准，完整架构以
[`docs/design.md`](docs/design.md) 为准。

部署、备份、恢复和故障排查见 [`docs/operations.md`](docs/operations.md)。

## 本地启动

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv\Scripts\python -m uvicorn minimal_agent.main:app --reload
```

编辑 `.env` 时必须填写 LLM Key、模型，兼容服务还要填写 base URL，并按实际模型填写
`CONTEXT_WINDOW_TOKENS`；保留为 `0` 时 Agent 消息接口会明确返回 503，但健康检查和
Session 查询仍可使用。生产式启动必须使用单 worker，完整命令见运行手册。

`LLM_API_STYLE` 支持 `responses` 和 `chat_completions`。官方 OpenAI 可留空
`LLM_BASE_URL`；其他 OpenAI-compatible 服务填写自己的端点。推荐只使用通用 `LLM_*`
变量，旧 `OPENAI_*`/`DEEPSEEK_*` 名称仅作为兼容别名。

健康检查：`GET http://127.0.0.1:8000/health`

## Vue 测试控制台

仓库包含一个用于本地验收 Session、工具调用、SSE 和 Trace 的 Vue 3 前端：

```powershell
cd frontend
npm install
npm run dev
```

保持后端运行在 `http://127.0.0.1:8000`，然后打开 `http://127.0.0.1:5173`。Vite 会在
开发环境代理 `/health` 和 `/v1`，无需额外配置 CORS。详细说明见
[`frontend/README.md`](frontend/README.md)。

HTTP API：

- `POST /v1/sessions`、`GET /v1/sessions`、`GET /v1/sessions/{session_id}`
- `GET /v1/sessions/{session_id}/messages`
- `POST /v1/sessions/{session_id}/messages`，请求体为 `{"text":"..."}`，返回 `202`
- `GET /v1/runs/{run_id}`、`GET /v1/runs/{run_id}/trace`
- `GET /v1/runs/{run_id}/events`，以 SSE 重放并跟随 Run

同一 Session 同时只允许一个 running Run；重复提交返回 `409 SESSION_BUSY`。所有 JSON
错误使用 `{"error":{"code","message","details"}}` 结构。缺少 LLM 配置时健康检查和
Session 查询仍可用，提交消息返回 `503 AGENT_UNAVAILABLE`。

本地主板搜索数据位于 `data/mainboards.json`，可通过 `MAINBOARD_DATA_PATH` 指向其他文件。
字段和更新规则见 [`data/README.md`](data/README.md)。

天气工具使用 Open-Meteo 地理编码和预报 API，不需要 API Key；运行时需要访问外网。
`WEATHER_HTTP_TIMEOUT_SECONDS` 同时限制连接、读取、写入和连接池等待时间。

Session Context 从 SQLite 重建，并在估算输入达到模型窗口的 80% 时压缩旧轮次。
`CONTEXT_WINDOW_TOKENS` 必须按实际模型显式配置；`SYSTEM_PROMPT_PATH`、
`SUMMARIZE_PROMPT_PATH` 可覆盖默认提示词文件。原始消息不会因摘要而删除。

Run 流通过 `GET /v1/runs/{run_id}/events` 以 SSE 输出。客户端断线重连时可发送标准
`Last-Event-ID` 请求头，也可使用 `after_id` 查询参数；后者同时存在时优先。心跳为
不落库的 SSE 注释，已提交的业务事件以 SQLite 自增 ID 重放。

推理流统一使用 `reasoning.started`、`reasoning.delta` 和 `reasoning.done`。系统可捕获
Responses 的 `reasoning_text`/`reasoning_summary_text`、兼容 Chat 接口的
`reasoning_content`，以及正文开头跨 chunk 的 `<think>...</think>`。`source` 标识来源，
`content_type` 区分 `raw`、`summary` 和 `activity`。模型没有返回可见推理文本时不会伪造。
推理事件和正文事件先批量写入 SQLite，再由独立 SSE 读取器重放。

`AgentLoop` 每次从当前 Session 重建 Context，消费 LLM 流并在“最终答案”或“单个工具
调用”之间作出严格决策；工具结果会持久化并进入下一步 Context。Run 默认最多 8 个模型
步骤、总时限 60 秒。`RunSupervisor` 持有后台任务强引用，并在关闭或进程重启恢复时把
未完成 Run 转为可重放的持久化失败终态。

## 通用真实模型测试

真实测试使用当前 `.env` 选择的模型和 API 风格，不绑定某个供应商。测试默认跳过，避免
普通测试意外产生费用。把 Key 仅放入本地 `.env` 或当前进程环境：

```powershell
$env:LLM_API_KEY="你的 Key"
$env:LLM_MODEL="供应商模型名"
$env:LLM_BASE_URL="兼容服务地址；官方 OpenAI 可不设"
$env:LLM_API_STYLE="responses"
$env:RUN_LIVE_LLM_TESTS="1"
$env:LIVE_TEST_PROFILE="smoke"
$env:LIVE_MAX_LLM_CALLS="1"
.venv\Scripts\python -m pytest -m live tests\live\test_llm_live.py -ra
```

`smoke` 最多调用一次。`full` 验证 calculator、search、Mock Open-Meteo weather、真实
摘要压缩和 reasoning，全局默认最多 8 次 LLM 调用：

```powershell
$env:LIVE_TEST_PROFILE="full"
$env:LIVE_MAX_LLM_CALLS="8"
$env:LIVE_MAX_OUTPUT_TOKENS="1024"
$env:LIVE_EXPECT_REASONING="raw" # raw | summary | activity | none
$env:LIVE_EXPECT_REASONING_SOURCE="either" # structured | tagged | either
.venv\Scripts\python -m pytest -m live tests\live\test_llm_live.py -ra
```

full profile 的默认单次输出上限是 1024 tokens；可按模型和成本预算显式调低，但过低可能
把正常的详细工具回答判定为 `LLM_RESPONSE_INCOMPLETE`。其中只有 LLM 是真实远端调用；
天气 HTTP 使用确定性 Mock，避免第三方天气服务
波动污染模型工具调用验收。不要把真实 Key 写入 `.env.example`、测试文件、Trace 或提交
记录。`LIVE_EXPECT_REASONING=none` 表示不要求模型返回可见推理，不会把隐藏 token 当成
测试失败。

## 质量检查

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m build
```

MVP 运行约束：单进程、单 Uvicorn worker。原因是 RunSupervisor 和事件通知器是
进程内组件；多进程部署需要外部任务队列和跨进程通知，不属于当前范围。

